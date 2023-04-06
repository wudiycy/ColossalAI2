import torch.nn as nn

from colossalai.booster.accelerator import Accelerator
from colossalai.testing import clear_cache_before_run, parameterize


@clear_cache_before_run()
@parameterize('device', ['cpu', 'cuda'])
def test_accelerator(device):
    acceleartor = Accelerator(device)
    model = nn.Linear(8, 8)
    model = acceleartor.configure_model(model)
    assert next(model.parameters()).device.type == device
    del model, acceleartor
