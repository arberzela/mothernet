import argparse
import os
import shutil
import socket
import sys
import time
from datetime import datetime
from pathlib import Path

import mlflow
import torch
from syne_tune import Reporter

from tabpfn.mlflow_utils import MLFLOW_HOSTNAME
from tabpfn.model_builder import get_model, save_model
from tabpfn.model_configs import get_base_config_paper
from tabpfn.utils import compare_dicts, argparser_from_config, init_device, get_model_string

def main(argv):
    config = get_base_config_paper()
    parser = argparser_from_config(config)
    args = parser.parse_args(argv)

    device, rank, num_gpus = init_device(args.gpu_id, args.use_cpu)

    # handle syne-tune restarts
    checkpoint_dir = args.st_checkpoint_dir
    if checkpoint_dir is not None:
        args.base_path = checkpoint_dir
        os.makedirs(checkpoint_dir, exist_ok=True)
        checkpoint_path = Path(checkpoint_dir) / "checkpoint.mothernet"
        if checkpoint_path.exists():
            args.continue_run = True
            args.load_file = checkpoint_path

    if args.create_new_run and not args.continue_run:
        raise ValueError("Specifying create-new-run makes no sense when not continuing run")
    base_path = args.base_path

    torch.set_num_threads(24)
    config.update(vars(args))
    config['num_gpus'] = 1
    config['device'] = device

    config['lr'] = args.learning_rate
    config['emsize'] = args.em_size
    config['aggregate_k_gradients'] = args.agg_gradients
    config['predicted_hidden_layers'] = args.num_predicted_hidden_layers

    warm_start_weights = args.load_file
    config['no_double_embedding'] = not args.double_embedding
    config['nhead'] = config['emsize'] // 128

    config['num_steps'] = args.num_steps or 1024 * 64 // config['batch_size'] // config['aggregate_k_gradients']
    config['weight_embedding_rank'] = args.weight_embedding_rank if args.low_rank_weights else None

    if config['model_maker'] == 'perceiver' and config['perceiver_large_dataset']:
        config['max_eval_pos'] = 8 * 1000
        config['n_samples'] = 8 * 1024+128

    if config['extra_fast_test']:
        config['max_eval_pos'] = 16
        config['n_samples'] = 2 * 16
        config['nhead'] = 1

    config['decoder_embed_dim'] = args.decoder_em_size
    config['warm_start_from'] = warm_start_weights
    config['continue_old_config'] = args.continue_run
    save_every = args.save_every

    model_state, optimizer_state, scheduler = None, None, None
    if warm_start_weights is not None:
        model_state, old_optimizer_state, old_scheduler, old_config = torch.load(
            warm_start_weights, map_location='cpu')
        module_prefix = 'module.'
        model_state = {k.replace(module_prefix, ''): v for k, v in model_state.items()}
        if args.continue_run:
            config = old_config
            config['device'] = device
            config['warm_start_from'] = warm_start_weights
            optimizer_state = old_optimizer_state
            config['stop_after_epochs'] = args.stop_after_epochs
            if not args.restart_scheduler:
                scheduler = old_scheduler
        else:
            print("WARNING warm starting with new settings")
            compare_dicts(config, old_config, all=True)

    report = Reporter()

    model_string = get_model_string(config, args, parser)

    def save_callback(model, optimizer, scheduler, epoch):
        if not hasattr(model, 'last_saved_epoch'):
            model.last_saved_epoch = 0
        log_file = f'{base_path}/log/{model_string}.log'
        if epoch == "start":
            print(f"Starting training of model {model_string}")
            return
        try:
            os.makedirs(f"{base_path}/log", exist_ok=True)
            with open(log_file, 'a') as f:
                f.write(f'Epoch {epoch} loss {model.losses[-1]} learning_rate {model.learning_rates[-1]}\n')
        except Exception as e:
            print(f'Failed to write to log file {log_file}: {e}')

        if epoch != "on_exit":
            wallclock_ticker = max(1, int(model.wallclock_times[-1]//(60 * 5)))
            if not args.no_mlflow:
                mlflow.log_metric(key="wallclock_time", value=model.wallclock_times[-1], step=epoch)
                mlflow.log_metric(key="loss", value=model.losses[-1], step=epoch)
                mlflow.log_metric(key="learning_rate", value=model.learning_rates[-1], step=epoch)
                mlflow.log_metric(key="wallclock_ticker", value=wallclock_ticker, step=epoch)
            report(epoch=epoch, loss=model.losses[-1], wallclock_time=wallclock_ticker)  # every 5 minutes

        try:
            if (epoch == "on_exit") or epoch % save_every == 0:
                if checkpoint_dir is not None:
                    if epoch == "on_exit":
                        return
                    file_name = f'{base_path}/checkpoint.mothernet'
                else:
                    file_name = f'{base_path}/models_diff/{model_string}_epoch_{epoch}.cpkt'
                os.makedirs(f"{base_path}/models_diff", exist_ok=True)
                disk_usage = shutil.disk_usage(f"{base_path}/models_diff")
                if disk_usage.free < 1024 * 1024 * 1024 * 2:
                    print("Not saving model, not enough disk space")
                    print("DISK FULLLLLLL")
                    return
                with open(log_file, 'a') as f:
                    f.write(f'Saving model to {file_name}\n')
                print(f'Saving model to {file_name}')
                config['epoch_in_training'] = epoch
                config['learning_rates'] = model.learning_rates
                config['losses'] = model.losses
                config['wallclock_times'] = model.wallclock_times

                save_model(model, optimizer, scheduler, base_path, file_name, config)
                # remove checkpoints that are worse than current
                if epoch != "on_exit" and epoch - save_every > 0:
                    this_loss = model.losses[-1]
                    for i in range(epoch // save_every):
                        loss = model.losses[i * save_every - 1]  # -1 because we start at epoch 1
                        old_file_name = f'{base_path}/models_diff/{model_string}_epoch_{i * save_every}.cpkt'
                        if os.path.exists(old_file_name):
                            if loss > this_loss:
                                try:
                                    print(f"Removing old model file {old_file_name}")
                                    os.remove(old_file_name)
                                except Exception as e:
                                    print(f"Failed to remove old model file {old_file_name}: {e}")
                            else:
                                print(f"Not removing old model file {old_file_name} because loss is too high ({loss} < {this_loss})")

        except Exception as e:
            print("WRITING TO MODEL FILE FAILED")
            print(e)

    if not args.no_mlflow:
        mlflow.set_tracking_uri(f"http://{MLFLOW_HOSTNAME}:5000")

        tries = 0
        while tries < 5:
            try:
                mlflow.set_experiment(args.experiment)
                break
            except:
                tries += 1
                print(f"Failed to set experiment, retrying {tries}/5")
                time.sleep(5)

        if args.continue_run and not args.create_new_run:
            # find run id via mlflow
            run_ids = mlflow.search_runs(filter_string=f"attribute.run_name='{model_string}'")['run_id']
            if len(run_ids) > 1:
                raise ValueError(f"Found more than one run with name {model_string}")
            run_id = run_ids.iloc[0]
            run_args = {'run_id': run_id}

        else:
            run_args = {'run_name': model_string}

        with mlflow.start_run(**run_args):
            mlflow.log_param('hostname', socket.gethostname())
            mlflow.log_params({k: v for k, v in config.items() if isinstance(v, (int, float, str)) and k != 'epoch_in_training'})
            total_loss, model, dl, epoch = get_model(config, device, should_train=True, verbose=1, epoch_callback=save_callback, model_state=model_state,
                                                     optimizer_state=optimizer_state, scheduler=scheduler,
                                                     load_model_strict=args.continue_run or args.load_strict)

    else:
        total_loss, model, dl, epoch = get_model(config, device, should_train=True, verbose=1, epoch_callback=save_callback, model_state=model_state,
                                                 optimizer_state=optimizer_state, scheduler=scheduler,
                                                 load_model_strict=args.continue_run or args.load_strict)

    if rank == 0:
        save_callback(model, None, None, "on_exit")
    return {'loss': total_loss, 'model': model, 'dataloader': dl,
            'config': config, 'base_path': base_path,
            'model_string': model_string, 'epoch': epoch}


if __name__ == "__main__":
    main(sys.argv[1:])
