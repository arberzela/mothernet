import socket
import sys
import time

import mlflow
import torch
import os

from git import Repo

from mothernet.model_builder import get_model
from mothernet.model_configs import get_base_config
from mothernet.utils import compare_dicts, init_device, get_model_string, synetune_handle_checkpoint, make_training_callback, flatten_dict
from mothernet.cli_parsing import argparser_from_config
from argparse import Namespace


def main(argv):
    config = get_base_config()
    parser = argparser_from_config(config)
    args = parser.parse_args(argv)
    device, rank, num_gpus = init_device(args.general.gpu_id, args.general.use_cpu)

    # handle syne-tune restarts
    orchestration = args.orchestration
    orchestration.base_path, orchestration.continue_run, orchestration.warm_start_from, report = synetune_handle_checkpoint(orchestration)

    if orchestration.create_new_run and not orchestration.continue_run:
        raise ValueError("Specifying create-new-run makes no sense when not continuing run")
    base_path = orchestration.base_path

    torch.set_num_threads(24)
    for group_name in vars(args):
        if group_name not in config:
            config[group_name] = {}
        for k, v in vars(getattr(args, group_name)).items():
            if isinstance(v, Namespace):
                if k not in config[group_name]:
                    config[group_name][k] = {}
                # FIXME we only allow one level of nesting, we should do recursion here really.
                config[group_name][k].update(vars(v))
            else:
                config[group_name][k] = v
        config[group_name].update()

    if args.orchestration.seed_everything:
        import lightning as L
        L.seed_everything(42)

    # promote general group to top level
    config.update(config.pop('general'))
    config['num_gpus'] = 1
    config['device'] = device

    warm_start_weights = orchestration.warm_start_from
    config['transformer']['nhead'] = config['transformer']['emsize'] // 128

    config['dataloader']['num_steps'] = config['dataloader']['num_steps'] or 1024 * \
        64 // config['dataloader']['batch_size'] // config['optimizer']['aggregate_k_gradients']

    if args.orchestration.extra_fast_test:
        config['dataloader']['max_eval_pos'] = 16
        config['prior']['n_samples'] = 2 * 16
        config['transformer']['nhead'] = 1

    save_every = orchestration.save_every

    model_state, optimizer_state, scheduler = None, None, None
    if warm_start_weights is not None:
        model_state, old_optimizer_state, old_scheduler, old_config = torch.load(
            warm_start_weights, map_location='cpu')
        module_prefix = 'module.'
        model_state = {k.replace(module_prefix, ''): v for k, v in model_state.items()}
        if args.orchestration.continue_run:
            config = old_config
            # we want to overwrite specific parts of the old config with current values
            config['device'] = device
            config['orchestration']['warm_start_from'] = warm_start_weights
            optimizer_state = old_optimizer_state
            config['orchestration']['stop_after_epochs'] = args.orchestration.stop_after_epochs
            if not args.orchestration.restart_scheduler:
                scheduler = old_scheduler
        else:
            print("WARNING warm starting with new settings")
            compare_dicts(config, old_config)

    model_string = get_model_string(args, parser, num_gpus, device)
    save_callback = make_training_callback(save_every, model_string, base_path, report, config, orchestration.no_mlflow, orchestration.st_checkpoint_dir)

    mlflow_hostname = os.environ.get("MLFLOW_HOSTNAME", None)
    if orchestration.no_mlflow or mlflow_hostname is None:
        print("Not logging run with mlflow, set MLFLOW_HOSTNAME environment variable enable mlflow.")
        total_loss, model, dl, epoch = get_model(config, device, should_train=True, verbose=1, epoch_callback=save_callback, model_state=model_state,
                                                 optimizer_state=optimizer_state, scheduler=scheduler,
                                                 load_model_strict=orchestration.continue_run or orchestration.load_strict)
    else:
        print(f"Logging run with mlflow at host {mlflow_hostname}")
        mlflow.set_tracking_uri(f"http://{mlflow_hostname}:5000")

        tries = 0
        while tries < 5:
            try:
                mlflow.set_experiment(orchestration.experiment)
                break
            except:
                tries += 1
                print(f"Failed to set experiment, retrying {tries}/5")
                time.sleep(5)

        if orchestration.continue_run and not orchestration.create_new_run:
            # find run id via mlflow
            run_ids = mlflow.search_runs(filter_string=f"attribute.run_name='{model_string}'")['run_id']
            if len(run_ids) > 1:
                raise ValueError(f"Found more than one run with name {model_string}")
            run_id = run_ids.iloc[0]
            run_args = {'run_id': run_id}

        else:
            run_args = {'run_name': model_string}

        path = os.path.dirname(os.path.abspath(__file__))
        run_args['tags'] = {'mlflow.source.git.commit': Repo(path, search_parent_directories=True).head.object.hexsha}

        with mlflow.start_run(**run_args):
            mlflow.log_param('hostname', socket.gethostname())
            mlflow.log_params(flatten_dict(config))
            total_loss, model, dl, epoch = get_model(config, device, should_train=True, verbose=1, epoch_callback=save_callback, model_state=model_state,
                                                     optimizer_state=optimizer_state, scheduler=scheduler,
                                                     load_model_strict=orchestration.continue_run or orchestration.load_strict)

    if rank == 0:
        save_callback(model, None, None, "on_exit")
    return {'loss': total_loss, 'model': model, 'dataloader': dl,
            'config': config, 'base_path': base_path,
            'model_string': model_string, 'epoch': epoch}


if __name__ == "__main__":
    main(sys.argv[1:])
