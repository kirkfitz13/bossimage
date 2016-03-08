# Copyright 2016 Joseph Wright <rjosephwright@gmail.com>
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.
from __future__ import print_function
import boto3 as boto
import functools as f
import json
import os
import random
import re
import shutil
import socket
import string
import subprocess
import time
import tempfile
import voluptuous as v
import yaml

def cached(func):
    cache = {}

    @f.wraps(func)
    def wrapper(*args, **kwargs):
        key = func.__name__ + str(sorted(args)) + str(sorted(kwargs.items()))
        if key not in cache:
            cache[key] = func(*args, **kwargs)
        return cache[key]
    return wrapper

@cached
def ec2_connect():
    session = boto.Session()
    return session.resource('ec2')

def snake_to_camel(s):
    return ''.join(part[0].capitalize() + part[1:] for part in s.split('_'))

def camelify(spec):
    if type(spec) == list:
        return [camelify(m) for m in spec]
    elif type(spec) == dict:
        return { snake_to_camel(k): camelify(v) for k, v in spec.items() }
    else:
        return spec

def gen_keyname():
    letters = string.ascii_letters + string.digits
    base = 'bossimage-'
    rand = ''.join([letters[random.randrange(0, len(letters))] for _ in range(10)])
    return base + rand

def create_working_dir():
    if not os.path.exists('.boss'): os.mkdir('.boss')

def create_instance(config, files, keyname):
    ec2 = ec2_connect()
    kp = ec2.create_key_pair(KeyName=keyname)

    with open(files['keyfile'], 'w') as f:
        f.write(kp.key_material)
    os.chmod(files['keyfile'], 0600)

    (instance,) = ec2.create_instances(
        ImageId=ami_id_for(config['image']),
        InstanceType=config['instance_type'],
        MinCount=1,
        MaxCount=1,
        KeyName=keyname,
        NetworkInterfaces=[dict(
            DeviceIndex=0,
            AssociatePublicIpAddress=True,
        )],
        BlockDeviceMappings=camelify(config['block_device_mappings']),
    )
    print('Created instance {}'.format(instance.id))

    instance.wait_until_running()
    print('Instance is running')

    instance.reload()
    return instance

def write_files(files, instance, keyname, config):
    with open(files['config'], 'w') as f:
        f.write(yaml.safe_dump(dict(
            id=instance.id,
            ip=instance.public_ip_address,
            keyname=keyname,
            platform=config['platform'],
        )))

    with open(files['inventory'], 'w') as f:
        inventory = '{} ansible_ssh_private_key_file={} ansible_user={}'.format(
            instance.public_ip_address,
            files['keyfile'],
            config['username'],
        )
        f.write(inventory)

    with open(files['playbook'], 'w') as f:
        f.write(yaml.safe_dump([dict(
            hosts='all',
            become=True,
            roles=[os.path.basename(os.getcwd())],
        )]))

def load_or_create_instance(config):
    instance = '{}-{}'.format(config['platform'], config['profile'])
    files = instance_files(instance)

    if not os.path.exists(files['config']):
        keyname = gen_keyname()
        instance = create_instance(config, files, keyname)
        write_files(files, instance, keyname, config)

    with open(files['config']) as f:
        return yaml.load(f)

def wait_for_image(image):
    while(True):
        image.reload()
        if image.state == 'available':
            break
        else:
            time.sleep(5)

def wait_for_ssh(addr):
    while(True):
        try:
            print('Attempting ssh connection to {} ... '.format(addr), end='')
            socket.create_connection((addr, 22), 1)
            print('ok')
            break
        except:
            print('failed, will retry')
            time.sleep(5)

def run(instance, extra_vars, verbosity):
    files = instance_files(instance)

    env = os.environ.copy()

    env.update(dict(ANSIBLE_ROLES_PATH='.boss/roles:..'))

    ansible_galaxy_args = ['ansible-galaxy', 'install', '-r', 'requirements.yml']
    if verbosity:
        ansible_galaxy_args.append('-' + 'v' * verbosity)
    ansible_galaxy = subprocess.Popen(ansible_galaxy_args, env=env)
    ansible_galaxy.wait()

    env.update(dict(ANSIBLE_HOST_KEY_CHECKING='False'))

    ansible_playbook_args = ['ansible-playbook', '-i', files['inventory']]
    if verbosity:
        ansible_playbook_args.append('-' + 'v' * verbosity)
    if extra_vars:
        ansible_playbook_args += ['--extra-vars', json.dumps(extra_vars)]
    ansible_playbook_args.append(files['playbook'])
    ansible_playbook = subprocess.Popen(ansible_playbook_args, env=env)
    ansible_playbook.wait()

def image(instance):
    files = instance_files(instance)
    with open(files['config']) as f:
        c = yaml.load(f)

    ec2 = ec2_connect()

    ec2_instance = ec2.Instance(id=c['id'])
    image = ec2_instance.create_image(Name=instance)
    print('Created image {}'.format(image.id))

    wait_for_image(image)
    print('Image is available')

def delete(instance):
    files = instance_files(instance)

    with open(files['config']) as f:
        c = yaml.load(f)

    ec2 = ec2_connect()

    ec2_instance = ec2.Instance(id=c['id'])
    ec2_instance.terminate()

    kp = ec2.KeyPair(name=c['keyname'])
    kp.delete()

    for f in files.values(): os.unlink(f)

def instance_files(instance):
    return dict(
        config='.boss/{}.yml'.format(instance),
        keyfile='.boss/{}.pem'.format(instance),
        inventory='.boss/{}.inventory'.format(instance),
        playbook='.boss/{}-playbook.yml'.format(instance),
    )

def ami_id_for(name):
    if name.startswith('ami-'): return name
    ec2 = ec2_connect()
    i = list(ec2.images.filter(
        Filters=[{ 'Name': 'name', 'Values': [name] }]
    ))
    if i: return i[0].id

def merge_config(c):
    merged = {}
    for platform in c['platforms']:
        for profile in c['profiles']:
            instance = '{}-{}'.format(platform['name'], profile['name'])
            merged[instance] = {
                k: v for k, v in platform.items() if k != 'name'
            }
            merged[instance]['platform'] = platform['name']
            merged[instance].update({
                k: v for k, v in c['driver'].items() if k not in platform
            })
            merged[instance].update({
                k: v for k, v in profile.items() if k != 'name'
            })
            merged[instance]['profile'] = profile['name']
    return merged

def invalid(kind, item):
    return v.Invalid('Invalid {}: {}'.format(kind, item))

def re_validator(pat, s, kind):
    if not re.match(pat, s): raise invalid(kind, s)
    return s

def coll_validator(coll, kind, thing):
    if thing not in coll: raise invalid(kind, thing)
    return thing

def is_subnet_id(s):
    return re_validator(r'subnet-[0-9a-f]{8}', s, 'subnet_id')

def is_snapshot_id(s):
    return re_validator(r'snap-[0-9a-f]{8}', s, 'snapshot_id')

def is_virtual_name(s):
    return re_validator(r'ephemeral\d+', s, 'virtual_name')

def is_volume_type(s):
    return coll_validator(('gp2', 'io1', 'standard'), 'volume_type', s)

def pre_merge_schema():
    default_profiles = [{
        'name': 'default',
        'extra_vars': {}
    }]
    return v.Schema({
        v.Optional('driver', default={}): { v.Extra: object },
        v.Required('platforms'): [{
            v.Required('name'): str,
        }],
        v.Optional('profiles', default=default_profiles): [{
            v.Required('name'): str,
        }],
    }, extra=v.ALLOW_EXTRA)

def post_merge_schema():
    return v.Schema({
        str: {
            'platform': str,
            'profile': str,
            'extra_vars': { v.Extra: object },
            v.Required('image'): str,
            v.Required('instance_type'): str,
            v.Optional('username', default='ec2-user'): str,
            v.Optional('block_device_mappings', default=[]): [{
                v.Required('device_name'): str,
                'ebs': {
                    'volume_size': int,
                    'volume_type': is_volume_type,
                    'delete_on_termination': bool,
                    'encrypted': bool,
                    'iops': int,
                    'snapshot_id': is_snapshot_id,
                },
                'no_device': str,
                'virtual_name': is_virtual_name,
            }],
        }
    })
