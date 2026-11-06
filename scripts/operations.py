"""Conservative deployment control: no automatic host-network replay."""
import hashlib
import json
import time
from pathlib import Path
from durable_state import atomic_json, read_json
from service_config import keys, load

RELOADABLE={'log_level','telemetry_seconds'}

def digest(config):
    return hashlib.sha256(json.dumps(config,sort_keys=True).encode()).hexdigest()

class Operations:
    def __init__(self,service,path,config_path):
        self.service=service
        self.path=path
        self.config_path=config_path
        self.reload_errors=0
        self.last_error=None
        self.checkpoint=None

    def start(self):
        old=read_json(self.path) if self.path.exists() else None
        if old is not None:
            keys(old,('version','generation','clean_shutdown','starts','config_hash','boot_id','network_replay'))
            if (old['version']!=1 or type(old['generation']) is not int or old['generation']<0 or
                type(old['starts']) is not int or old['starts']<1 or type(old['clean_shutdown']) is not bool or
                old['network_replay'] is not False):
                raise ValueError('invalid recovery checkpoint')
        self.previous_unclean=bool(old and not old['clean_shutdown'])
        self.checkpoint=dict(version=1,generation=(old['generation']+int(old['config_hash']!=digest(self.service.config))) if old else 0,
            starts=(old['starts']+1 if old else 1),clean_shutdown=False,
            config_hash=digest(self.service.config),
            boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip(),network_replay=False)
        self.save()

    def save(self):
        atomic_json(self.path,self.checkpoint)

    def reload(self,dry_run=False):
        try:
            cfg=load(self.config_path)
            changed=sorted(key for key in cfg if cfg[key]!=self.service.config[key])
            incompatible=set(changed)-RELOADABLE
            if incompatible:
                raise ValueError('restart required for configuration fields: '+','.join(sorted(incompatible)))
            if dry_run or not changed:
                return dict(valid=True,changed=changed,generation=self.checkpoint['generation'],dry_run=dry_run)
            checkpoint=dict(self.checkpoint,generation=self.checkpoint['generation']+1,config_hash=digest(cfg))
            def commit():
                self.service.config=cfg
                self.checkpoint=checkpoint
            atomic_json(self.path,checkpoint,on_commit=commit)
            self.last_error=None
            return dict(valid=True,changed=changed,generation=checkpoint['generation'],durable=True)
        except (OSError,ValueError,RuntimeError) as error:
            self.reload_errors+=1;self.last_error=str(error)
            raise

    def finish(self):
        self.checkpoint=dict(self.checkpoint,clean_shutdown=True)
        self.save()

    def status(self):
        lab=self.service.lab
        return dict(checkpoint=self.checkpoint,previous_unclean=self.previous_unclean,
            reload_errors=self.reload_errors,last_error=self.last_error,
            reconciliation='observe_only_no_kernel_replay',
            owned_lab_active=lab is not None,
            host_mutations_enabled=False,
            policy_revision=self.service.platform_policies.state['revision'],
            controller_state=self.service.controller.state.name,
            uptime_seconds=time.monotonic()-self.service.started)
