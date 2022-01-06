import contextlib
import os

import colossalai
import torch
from colossalai.core import global_context as gpc
from colossalai.engine.schedule import (InterleavedPipelineSchedule, PipelineSchedule)
from colossalai.logging import get_dist_logger
from colossalai.nn import CosineAnnealingWarmupLR
from colossalai.trainer import Trainer, hooks
from colossalai.utils import MultiTimer, get_dataloader
from colossalai.zero import zero3_model_context
from model_zoo.gpt import GPTLMLoss, gpt2_small, gpt2_medium, gpt2_large, gpt2_xl, gpt2_4B, gpt2_6B, gpt2_10B

# from data import WebtextDataset
from random_data_loader import get_random_data_loader
from model_size_calc import get_model_numel


def train_gpt():
    args = colossalai.get_default_parser().parse_args()
    # standard launch
    # colossalai.launch(config=args.config,
    #                   rank=args.rank,
    #                   world_size=args.world_size,
    #                   local_rank=args.local_rank,
    #                   host=args.host,
    #                   port=args.port)

    # launch from torchrun
    colossalai.launch_from_torch(config='./config/2p5d.py')

    logger = get_dist_logger()
    if hasattr(gpc.config, 'LOG_PATH'):
        if gpc.get_global_rank() == 0:
            log_path = gpc.config.LOG_PATH
            if not os.path.exists(log_path):
                os.mkdir(log_path)
            logger.log_to_file(log_path)

    # train_dataset = WebtextDataset(os.environ['DATA'], seq_len=gpc.config.SEQ_LENGTH)
    train_dataloader = get_random_data_loader(gpc.config.BATCH_SIZE, gpc.config.BATCH_SIZE * 10, gpc.config.SEQ_LENGTH, torch.device('cpu'), torch.float)
    # print(f'train_dataset len {len(train_dataset)} bs {gpc.config.BATCH_SIZE}')
    # train_dataloader = get_dataloader(train_dataset,
    #                                   seed=42,
    #                                   batch_size=gpc.config.BATCH_SIZE // gpc.data_parallel_size,
    #                                   pin_memory=True,
    #                                   shuffle=True,
    #                                   drop_last=True)
    # logger.info(f'Loaded {len(train_dataset)}/{len(train_dataloader)} samples/batches', ranks=[0])

    # zero3 under test
    # use_zero3 = hasattr(gpc.config, 'zero') and gpc.config.zero.level == 3
    # cm = zero3_model_context() if use_zero3 else contextlib.nullcontext()
    # with cm:
    #     model = gpc.config.model.pop('type')(**gpc.config.model)

    model = gpt2_10B(vocab_size=gpc.config.VOCAB_SIZE,
                        max_position_embeddings=gpc.config.SEQ_LENGTH,
                        checkpoint=True)
    model_numel, param_cnt = get_model_numel(model)
    logger.info(f'model numel {model_numel / 1024**3} M param cnt {param_cnt}')
    criterion = GPTLMLoss()

    optimizer = torch.optim.Adam(model.parameters(), lr=0.00015, weight_decay=1e-2)

    steps_per_epoch = len(train_dataloader) // gpc.config.gradient_accumulation

    lr_scheduler = CosineAnnealingWarmupLR(optimizer=optimizer,
                                           total_steps=gpc.config.NUM_EPOCHS * steps_per_epoch,
                                           warmup_steps=gpc.config.WARMUP_EPOCHS * steps_per_epoch,
                                           eta_min=1e-5)

    engine, train_dataloader, _, lr_scheduler = colossalai.initialize(model=model,
                                                                      optimizer=optimizer,
                                                                      criterion=criterion,
                                                                      train_dataloader=train_dataloader,
                                                                      lr_scheduler=lr_scheduler)

    # pipeline under test
    # num_model_chunks = getattr(gpc.config.model, 'num_chunks', 1)
    # if num_model_chunks > 1:
    #     logger.info('Build InterleavedPipelineSchedule', ranks=[0])
    #     schedule = InterleavedPipelineSchedule(gpc.config.NUM_MICRO_BATCHES, num_model_chunks)
    # else:
    #     logger.info('Build PipelineSchedule', ranks=[0])
    #     schedule = PipelineSchedule(gpc.config.NUM_MICRO_BATCHES)

    timer = MultiTimer()

    trainer = Trainer(engine=engine, logger=logger, timer=timer)

    hook_list = [
        hooks.LogMetricByEpochHook(logger=logger),
        hooks.LogMetricByStepHook(),
        hooks.LossHook(),
        hooks.ThroughputHook(),
        hooks.LRSchedulerHook(lr_scheduler=lr_scheduler, by_epoch=False),
        # hooks.TensorboardHook(log_dir='./tb_logs', ranks=[0]),
        # hooks.LogMemoryByEpochHook(logger),
        # hooks.LogTimingByEpochHook(timer, logger),
        # hooks.SaveCheckpointHook(checkpoint_dir='./ckpt')
    ]

    logger.info("Training start", ranks=[0])
    trainer.fit(train_dataloader=train_dataloader, epochs=gpc.config.NUM_EPOCHS, hooks=hook_list, display_progress=True)


if __name__ == '__main__':
    train_gpt()
