from attr import attrs, attrib
import subprocess as sp
import time
import json
import yaml
import tempfile
import argparse
import scannerpy
import os
import signal
import shlex
import cloudpickle
import base64
import math
import traceback
from abc import ABC

MASTER_POOL = 'default-pool'
WORKER_POOL = 'workers'


def run(s, detach=False):
    if detach:
        sp.Popen(s, shell=True, stdout=sp.PIPE, stderr=sp.PIPE)
        return None
    else:
        return sp.check_output(s, shell=True).decode('utf-8').strip()


@attrs
class CloudConfig:
    project = attrib(type=str)
    service_key = attrib(type=str)
    storage_key_id = attrib(type=str)
    storage_key_secret = attrib(type=str)

    @service_key.default
    def _service_key_default(self):
        return os.environ['GOOGLE_APPLICATION_CREDENTIALS']

    @storage_key_id.default
    def _storage_key_id_default(self):
        return os.environ['AWS_ACCESS_KEY_ID']

    @storage_key_secret.default
    def _storage_key_secret_default(self):
        return os.environ['AWS_SECRET_ACCESS_KEY']


class MachineType(ABC):
    def get_cpu(self):
        raise NotImplemented

    def get_mem(self):
        raise NotImplemented

    def get_name(self):
        raise NotImplemented


GCP_MEM_RATIOS = {'standard': 3.75, 'highmem': 6.5, 'highcpu': 0.9}


@attrs
class MachineTypeName(MachineType):
    name = attrib(type=str)

    def get_cpu(self):
        return int(self.name.split('-')[2])

    def get_mem(self):
        ty = self.name.split('-')[1]
        return int(GCP_MEM_RATIOS[ty] * self.get_cpu())

    def get_name(self):
        return self.name


@attrs
class MachineTypeQuantity(MachineType):
    cpu = attrib(type=int)
    mem = attrib(type=int)

    def get_cpu(self):
        return self.cpu

    def get_mem(self):
        return self.mem

    def get_name(self):
        # See Google Cloud documentation for instance names.
        # https://cloud.google.com/compute/pricing#machinetype
        name = None
        mem_cpu_ratio = float(self.mem) / self.cpu
        if math.log2(self.cpu).is_integer():
            for k, ratio in GCP_MEM_RATIOS.items():
                if math.isclose(mem_cpu_ratio, ratio):
                    name = 'n1-{}-{}'.format(k, self.cpu)

        if name is None:
            name = 'custom-{}-{}'.format(self.cpu, self.mem * 1024)
            if mem_cpu_ratio > 6.5:
                name += '-ext'

        return name


@attrs
class MachineConfig:
    type = attrib(type=MachineType)
    disk = attrib(type=int)
    gpu = attrib(type=int, default=0)
    gpu_type = attrib(type=str, default='nvidia-tesla-p100')
    preemptible = attrib(type=bool, default=False)

    def price(self):
        parts = self.type.get_name().split('-')
        category = parts[0]
        preempt = 'preemptible' if self.preemptible else 'normal'
        p = 0

        if category == 'n1':
            prices = {
                'normal': {
                    'standard': 0.0475,
                    'highmem': 0.1184 / 2,
                    'highcpu': 0.0709 / 2
                },
                'preemptible': {
                    'standard': 0.01,
                    'highmem': 0.0125,
                    'highcpu': 0.0075
                }
            }
            p = prices[preempt][parts[1]] * int(parts[2])

        elif category == 'custom':
            [cpu, mem] = parts[1:3]
            cpu = int(cpu)
            mem = int(mem)
            prices = {
                'normal': {
                    'cpu': 0.033174,
                    'mem': 0.004446,
                },
                'preemptible': {
                    'cpu': 0.00698,
                    'mem': 0.00094
                }
            }
            p = prices[preempt]['cpu'] * cpu + prices[preempt]['mem'] * (mem / 1024)

        else:
            raise Exception('Invalid category {}'.format(category))

        gpu_prices = {
            'normal': {
                'nvidia-tesla-k80': 0.45,
                'nvidia-tesla-p100': 1.46,
                'nvidia-tesla-v100': 2.48
            },
            'preemptible': {
                'nvidia-tesla-k80': 0.135,
                'nvidia-tesla-p100': 0.43,
                'nvidia-tesla-v100': 0.74
            }
        }

        p += gpu_prices[preempt][self.gpu_type] * self.gpu
        return p


@attrs
class ClusterConfig:
    id = attrib(type=str)
    num_workers = attrib(type=int)
    master = attrib(type=MachineConfig)
    worker = attrib(type=MachineConfig)
    zone = attrib(type=str, default='us-east1-b')
    kube_version = attrib(type=str, default='1.9.7-gke.3')
    workers_per_node = attrib(type=int, default=1)
    autoscale = attrib(type=bool, default=False)
    no_workers_timeout = attrib(type=int, default=600)
    scopes = attrib(
        type=list,
        default=[
            "https://www.googleapis.com/auth/compute",
            "https://www.googleapis.com/auth/devstorage.read_write",
            "https://www.googleapis.com/auth/logging.write",
            "https://www.googleapis.com/auth/monitoring", "https://www.googleapis.com/auth/pubsub",
            "https://www.googleapis.com/auth/servicecontrol",
            "https://www.googleapis.com/auth/service.management.readonly",
            "https://www.googleapis.com/auth/trace.append"
        ])
    scanner_config = attrib(
        type=str, default=os.path.join(os.environ['HOME'], '.scanner/config.toml'))
    pipelines = attrib(type=list, default=[])


class Cluster:
    def __init__(self, cloud_config, cluster_config):
        self._cloud_config = cloud_config
        self._cluster_config = cluster_config
        self._cluster_cmd = 'gcloud container --project {} clusters --zone {}' \
            .format(self._cloud_config.project, self._cluster_config.zone)

    def get_kube_info(self, kind, namespace='default'):
        return json.loads(run('kubectl get {} -o json -n {}'.format(kind, namespace)))

    def get_by_owner(self, ty, owner, namespace='default'):
        return run(
            'kubectl get {} -o json -n {} | jq \'.items[] | select(.metadata.ownerReferences[0].name == "{}") | .metadata.name\'' \
            .format(ty, namespace, owner)[1:-1])

    def get_object(self, info, name):
        for item in info['items']:
            if item['metadata']['name'] == name:
                return item
        return None

    def get_pod(self, deployment, namespace='default'):
        while True:
            rs = self.get_by_owner('rs', deployment, namespace)
            pod_name = self.get_by_owner('pod', rs, namespace)
            if "\n" not in pod_name and pod_name != "":
                break
            time.sleep(1)

        while True:
            pod = self.get_object(self.get_kube_info('pod', namespace), pod_name)
            if pod is not None:
                return pod
            time.sleep(1)

    def running(self, pool=MASTER_POOL):
        return run(
            '{cmd} list --cluster={id} --format=json | jq \'.[] | select(.name == "{pool}")\''.
            format(
                cmd=self._cluster_cmd.replace('clusters', 'node-pools'),
                id=self._cluster_config.id,
                pool=pool)) != ''

    def get_credentials(self):
        run('{cmd} get-credentials {id}'.format(cmd=self._cluster_cmd, id=self._cluster_config.id))

    def create_object(self, template):
        with tempfile.NamedTemporaryFile() as f:
            f.write(yaml.dump(template).encode())
            f.flush()
            run('kubectl create -f {}'.format(f.name))

    def make_container(self, name):
        template = {
            'name': name,
            'image': 'gcr.io/{project}/scanner-{name}:{device}'.format(
                project=self._cloud_config.project,
                name=name,
                device='gpu' if self._cluster_config.worker.gpu > 0 else 'cpu'),
            'command': ['/bin/bash'],
            'args': ['-c', 'python3 -c "from scannertools import kube; kube.{}()"'.format(name)],
            'imagePullPolicy': 'Always',
            'volumeMounts': [{
                'name': 'service-key',
                'mountPath': '/secret'
            }, {
                'name': 'scanner-config',
                'mountPath': '/root/.scanner/config.toml',
                'subPath': 'config.toml'
            }],
            'env': [
                {'name': 'GOOGLE_APPLICATION_CREDENTIALS',
                 'value': '/secret/{}'.format(os.path.basename(self._cloud_config.service_key))},
                {'name': 'AWS_ACCESS_KEY_ID',
                 'valueFrom': {'secretKeyRef': {
                     'name': 'aws-storage-key',
                     'key': 'AWS_ACCESS_KEY_ID'
                 }}},
                {'name': 'AWS_SECRET_ACCESS_KEY',
                 'valueFrom': {'secretKeyRef': {
                     'name': 'aws-storage-key',
                     'key': 'AWS_SECRET_ACCESS_KEY'
                 }}},
                {'name': 'NO_WORKERS_TIMEOUT',
                 'value': str(self._cluster_config.no_workers_timeout)},
                {'name': 'GLOG_minloglevel',
                 'value': '0'},
                {'name': 'GLOG_logtostderr',
                 'value': '1'},
                {'name': 'GLOG_v',
                 'value': '1'},
                {'name': 'WORKERS_PER_NODE',
                 'value': str(self._cluster_config.workers_per_node)},
                {'name': 'PIPELINES',
                 'value': base64.b64encode(cloudpickle.dumps(self._cluster_config.pipelines))},
                # HACK(wcrichto): GPU decode for interlaced videos is broken, so forcing CPU
                # decode instead for now.
                {'name': 'FORCE_CPU_DECODE',
                 'value': '1'}
            ],
            'resources': {},
            'securityContext': {'capabilities': {
                'add': ['SYS_PTRACE']  # Allows gdb to work in container
            }}
        }  # yapf: disable
        if name == 'master':
            template['ports'] = [{
                'containerPort': 8080,
            }]

        if self._cluster_config.worker.gpu > 0:
            template['resources']['limits'] = {'nvidia.com/gpu': self._cluster_config.worker.gpu}
        else:
            if name == 'worker':
                template['resources']['requests'] = {
                    'cpu': self._cluster_config.worker.type.get_cpu() / 2.0 + 0.1
                }

        return template

    def make_deployment(self, name, replicas):
        template = {
            'apiVersion': 'apps/v1beta1',
            'kind': 'Deployment',
            'metadata': {'name': 'scanner-{}'.format(name)},
            'spec': {  # DeploymentSpec
                'replicas': replicas,
                'template': {
                    'metadata': {'labels': {'app': 'scanner-{}'.format(name)}},
                    'spec': {  # PodSpec
                        'containers': [self.make_container(name)],
                        'volumes': [{
                            'name': 'service-key',
                            'secret': {
                                'secretName': 'service-key',
                                'items': [{
                                    'key': os.path.basename(self._cloud_config.service_key),
                                    'path': os.path.basename(self._cloud_config.service_key)
                                }]
                            }
                        }, {
                            'name': 'scanner-config',
                            'configMap': {'name': 'scanner-config'}
                        }],
                        'nodeSelector': {
                            'cloud.google.com/gke-nodepool':
                            MASTER_POOL if name == 'master' else WORKER_POOL
                        }
                    }
                }
            }
        }  # yapf: disable

        return template

    def _cluster_start(self):
        cfg = self._cluster_config
        fmt_args = {
            'cmd': self._cluster_cmd,
            'cluster_id': cfg.id,
            'cluster_version': cfg.kube_version,
            'master_machine': cfg.master.type.get_name(),
            'master_disk': cfg.master.disk,
            'worker_machine': cfg.worker.type.get_name(),
            'worker_disk': cfg.worker.disk,
            'scopes': ','.join(cfg.scopes),
            'initial_size': cfg.num_workers,
            'accelerator': '--accelerator type={},count={}'.format(cfg.worker.gpu_type, cfg.worker.gpu) if cfg.worker.gpu > 0 else '',
            'preemptible': '--preemptible' if cfg.worker.preemptible else '',
            'autoscaling': '--enable-autoscaling --min-nodes 0 --max-nodes {}'.format(cfg.num_workers) if cfg.autoscale else '',
            'master_cpu_platform': '--min-cpu-platform skylake' if cfg.master.type.get_cpu() > 64 else '',
            'worker_cpu_platform': '--min-cpu-platform skylake' if cfg.worker.type.get_cpu() > 64 else ''
        }  # yapf: disable

        cluster_cmd = """
{cmd} -q create "{cluster_id}" \
        --cluster-version "{cluster_version}" \
        --machine-type "{master_machine}" \
        --image-type "COS" \
        --disk-size "{master_disk}" \
        --scopes {scopes} \
        --num-nodes "1" \
        --enable-cloud-logging \
        {accelerator} \
        {master_cpu_platform}
        """.format(**fmt_args)

        if not self.running(pool=MASTER_POOL):
            try:
                price = cfg.master.price() + cfg.worker.price() * cfg.num_workers
                print('Cluster price: ${:.2f}/hr'.format(price))
            except Exception:
                print('Failed to compute cluster price with error:')
                traceback.print_exc()

            print('Creating master...')
            run(cluster_cmd)
            print(
                'https://console.cloud.google.com/kubernetes/clusters/details/{zone}/{cluster_id}?project={project}&tab=details' \
                .format(zone=cfg.zone, project=self._cloud_config.project, **fmt_args))

            fmt_args['cmd'] = fmt_args['cmd'].replace('clusters', 'node-pools')
            pool_cmd = """
    {cmd} -q create workers \
            --cluster "{cluster_id}" \
            --machine-type "{worker_machine}" \
            --image-type "COS" \
            --disk-size "{worker_disk}" \
            --scopes {scopes} \
            --num-nodes "{initial_size}" \
            {autoscaling} \
            {preemptible} \
            {accelerator} \
            {worker_cpu_platform}
            """.format(**fmt_args)

            print('Creating workers...')
            run(pool_cmd, detach=True)

        # TODO(wcrichto): run this if GPU driver daemon isn't running yet
        if cfg.worker.gpu > 0:
            # Install GPU drivers
            # https://cloud.google.com/kubernetes-engine/docs/concepts/gpus#installing_drivers
            run('kubectl apply -f https://raw.githubusercontent.com/GoogleCloudPlatform/container-engine-accelerators/stable/nvidia-driver-installer/cos/daemonset-preloaded.yaml'
                )

    def _kube_start(self, reset):
        cfg = self._cluster_config
        deploy = self.get_object(self.get_kube_info('deployments'), 'scanner-worker')
        if deploy is not None and cfg.num_workers == 1:
            num_workers = deploy['status']['replicas']
        else:
            num_workers = cfg.num_workers

        if reset:
            print('Deleting current deployments...')
            run('kubectl delete deploy/scanner-master deploy/scanner-worker --ignore-not-found=true'
                )
            run('kubectl delete service/scanner-master --ignore-not-found=true')

        secrets = self.get_kube_info('secrets')
        if self.get_object(secrets, 'service-key') is None:
            print('Making secrets...')
            run('kubectl create secret generic service-key --from-file={}' \
                .format(self._cloud_config.service_key))

        if self.get_object(secrets, 'aws-storage-key') is None:
            run('kubectl create secret generic aws-storage-key --from-literal=AWS_ACCESS_KEY_ID={} --from-literal=AWS_SECRET_ACCESS_KEY={}' \
                .format(self._cloud_config.storage_key_id, self._cloud_config.storage_key_secret))

        configmaps = self.get_kube_info('configmaps')
        if self.get_object(configmaps, 'scanner-config') is None:
            run('kubectl create configmap scanner-config --from-file={}' \
                .format(self._cluster_config.scanner_config))

        deployments = self.get_kube_info('deployments')
        if self.get_object(deployments, 'scanner-master') is None:
            print('Creating deployments...')
            self.create_object(self.make_deployment('master', 1))

        services = self.get_kube_info('services')
        if self.get_object(services, 'scanner-master') is None:
            run('kubectl expose deploy/scanner-master --type=NodePort --port=8080')

        if self.get_object(deployments, 'scanner-worker') is None:
            self.create_object(self.make_deployment('worker', num_workers))

    def start(self, reset=True):
        self._cluster_start()

        if self._cluster_config.id not in run(
                'kubectl config view -o json | jq \'.["current-context"]\' -r'):
            self.get_credentials()

        self._kube_start(reset)

    def resize(self, size):
        print('Resizing cluster...')
        if not self._cluster_config.autoscale:
            run('{cmd} resize {id} -q --node-pool=workers --size={size}' \
                .format(cmd=self._cluster_cmd, id=self._cluster_config.id, size=size))
        else:
            run('{cmd} update {id} -q --node-pool=workers --enable-autoscaling --max-nodes={size}' \
                .format(cmd=self._cluster_cmd, id=self._cluster_config.id, size=size))

        print('Scaling deployment...')
        run('kubectl scale deploy/scanner-worker --replicas={}'.format(size))

    def delete(self):
        run('{cmd} delete {id}'.format(cmd=self._cluster_cmd, id=self._cluster_config.id))

    def master_address(self):
        ip = run('''
            kubectl get pods -l 'app=scanner-master' -o json | \
            jq '.items[0].spec.nodeName' -r | \
            xargs -I {} kubectl get nodes/{} -o json | \
            jq '.status.addresses[] | select(.type == "ExternalIP") | .address' -r
            ''')

        port = run('''
            kubectl get svc/scanner-master -o json | \
            jq '.spec.ports[0].nodePort' -r
            ''')

        return '{}:{}'.format(ip, port)

    def database(self):
        return scannerpy.Database(master=self.master_address(), start_cluster=False)

    def job_status(self):
        db = self.database()
        jobs = db.get_active_jobs()
        if len(jobs) > 0:
            db.wait_on_job(jobs[0])

    def cli(self):
        parser = argparse.ArgumentParser()
        command = parser.add_subparsers(dest='command')
        command.required = True
        create = command.add_parser('start')
        create.add_argument(
            '--no-reset', '-nr', action='store_true', help='Delete current deployments')
        create.add_argument(
            '--num-workers', '-n', type=int, default=1, help='Initial number of workers')
        command.add_parser('delete')
        resize = command.add_parser('resize')
        resize.add_argument('size', type=int, help='Number of nodes')
        command.add_parser('get-credentials')
        command.add_parser('job-status')

        args = parser.parse_args()
        if args.command == 'start':
            self.start(reset=not args.no_reset)

        elif args.command == 'delete':
            self.delete()

        elif args.command == 'resize':
            self.resize(args.size)

        elif args.command == 'get-credentials':
            self.get_credentials()

        elif args.command == 'job-status':
            self.job_status()


def master():
    print('Scannertools: starting master...')
    scannerpy.start_master(
        port='8080',
        block=True,
        watchdog=False,
        no_workers_timeout=int(os.environ['NO_WORKERS_TIMEOUT']))


def worker():
    print('Scannertools: fetching resources...')
    pipelines = cloudpickle.loads(base64.b64decode(os.environ['PIPELINES']))
    for pipeline in pipelines:
        pipeline(None).fetch_resources()

    print('Scannertools: starting worker...')
    num_workers = int(os.environ['WORKERS_PER_NODE'])
    scannerpy.start_worker(
        '{}:{}'.format(os.environ['SCANNER_MASTER_SERVICE_HOST'],
                       os.environ['SCANNER_MASTER_SERVICE_PORT']),
        block=True,
        watchdog=False,
        port=5002,
        num_workers=num_workers if num_workers > 1 else None)
