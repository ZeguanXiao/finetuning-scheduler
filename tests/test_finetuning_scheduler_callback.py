# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import os
import re
from collections import OrderedDict
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pytest
import torch
import torch.nn.functional as F
import yaml
from pytorch_lightning import LightningModule, seed_everything, Trainer
from pytorch_lightning.callbacks import Callback, EarlyStopping
from pytorch_lightning.utilities import _StrategyType
from pytorch_lightning.utilities.cloud_io import get_filesystem
from pytorch_lightning.utilities.exceptions import MisconfigurationException
from torch import nn
from torch.utils.data import DataLoader, Dataset

from finetuning_scheduler import CallbackResolverMixin, FinetuningScheduler, FTSCheckpoint, FTSEarlyStopping
from tests.helpers import BoringModel
from tests.helpers.runif import RunIf

fts_resolver = CallbackResolverMixin()


def get_fts(trainer: "Trainer") -> Callback:
    fts_resolver.connect_callback(trainer, reconnect=True)
    return fts_resolver.finetuningscheduler_callback


class AverageDataset(Dataset):
    def __init__(self, dataset_len=300, sequence_len=100):
        self.dataset_len = dataset_len
        self.sequence_len = sequence_len
        self.input_seq = torch.randn(dataset_len, sequence_len, 10)
        top, bottom = self.input_seq.chunk(2, -1)
        self.output_seq = top + bottom.roll(shifts=1, dims=-1)

    def __len__(self):
        return self.dataset_len

    def __getitem__(self, item):
        return self.input_seq[item], self.output_seq[item]


class ParityModuleRNN(LightningModule):
    def __init__(self):
        super().__init__()
        self.rnn = nn.LSTM(10, 20, batch_first=True)
        self.linear_out = nn.Linear(in_features=20, out_features=5)
        self.example_input_array = torch.rand(2, 3, 10)

    def forward(self, x):
        seq, last = self.rnn(x)
        return self.linear_out(seq)

    def training_step(self, batch, batch_nb):
        x, y = batch
        y_hat = self(x)
        loss = F.mse_loss(y_hat, y)
        return {"loss": loss}

    def configure_optimizers(self):
        return torch.optim.Adam(self.parameters(), lr=0.02)

    def train_dataloader(self):
        return DataLoader(AverageDataset(), batch_size=30)


class ConvBlock(nn.Module):
    def __init__(self, in_channels, out_channels):
        super().__init__()
        self.conv = nn.Conv2d(in_channels, out_channels, 3)
        self.act = nn.ReLU()
        self.bn = nn.BatchNorm2d(out_channels)

    def forward(self, x):
        x = self.conv(x)
        x = self.act(x)
        return self.bn(x)


class ConvBlockParam(nn.Module):
    def __init__(self, in_channels, out_channels):
        super().__init__()
        self.module_dict = nn.ModuleDict({"conv": nn.Conv2d(in_channels, out_channels, 3), "act": nn.ReLU()})
        # add trivial test parameter to convblock to validate parent (non-leaf) module parameter handling
        self.parent_param = nn.Parameter(torch.zeros((1), dtype=torch.float))
        self.bn = nn.BatchNorm2d(out_channels)

    def forward(self, x):
        x = self.module_dict["conv"](x)
        x = self.module_dict["act"](x)
        return self.bn(x)


class FinetuningSchedulerBoringModel(BoringModel):
    """Extend :class:`~tests.helpers.BoringModel` to facilitate testing of
    :class:`~finetuning_scheduler.FinetuningScheduler` by ensuring deterministic divergence
    and accommodating no_decay list configuration"""

    def __init__(self, diverge_on_epoch: int = 3, no_decay: Optional[List] = None, weight_decay: float = 1.0e-06):
        super().__init__()
        self.layer = nn.Sequential(nn.Linear(32, 32), nn.Linear(32, 32), nn.Linear(32, 32), nn.Linear(32, 2))
        self.diverge_on_epoch = diverge_on_epoch
        self.no_decay = no_decay
        self.weight_decay = weight_decay

    def validation_step(self, batch, batch_idx):
        output = self(batch)
        loss = self.val_loss(batch, output)
        self.log("val_loss", loss, prog_bar=False)
        return {"x": loss}

    def val_loss(self, batch, prediction):
        # Make arbitrary val_loss the inverse of train_loss so val_loss diverges when desired
        val_func = (
            torch.zeros_like(prediction) if self.current_epoch >= self.diverge_on_epoch else torch.ones_like(prediction)
        )
        return torch.nn.functional.mse_loss(prediction, val_func)

    def configure_optimizers(self):
        parameters = filter(lambda x: x.requires_grad, self.parameters())
        optimizer = torch.optim.SGD(parameters, lr=1e-3, weight_decay=self.weight_decay)
        lr_scheduler = torch.optim.lr_scheduler.StepLR(optimizer, step_size=1, gamma=0.7)
        return [optimizer], [lr_scheduler]


class TestFinetuningScheduler(FinetuningScheduler):
    """Extends :class:`~finetuning_scheduler.FinetuningScheduler` to facilitate intra- fit state inspection during
    testing of scheduled finetuning."""

    def __init__(self, expected_state: Optional[Dict] = None, mock_strategy_wcpu: bool = False, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.expected_state = expected_state
        self.mock_strategy_wcpu = mock_strategy_wcpu
        self.best_ckpt_test_weight = None
        self.restored_best_cnt = 0
        self.was_global_zero = True

    def setup(self, trainer, pl_module, stage: Optional[str] = None) -> None:
        if self.mock_strategy_wcpu:
            trainer.strategy.strategy_name = _StrategyType.DDP2
        return super().setup(trainer, pl_module, stage)

    def state_dict(self) -> Dict[str, Any]:
        self.best_ckpt_test_weight = self.pl_module._modules["layer"]._modules["3"].bias.data.detach().clone()
        return super().state_dict()

    def restore_best_ckpt(self) -> None:
        super().restore_best_ckpt()
        assert torch.equal(self.pl_module._modules["layer"]._modules["3"].bias.data, self.best_ckpt_test_weight)
        self.restored_best_cnt += 1

    def on_train_epoch_start(self, trainer, pl_module):
        super().on_train_epoch_start(trainer, pl_module)
        state_key = trainer.current_epoch
        current_state = (
            self.curr_depth,
            self.depth_remaining,
            self._fts_state._ft_epoch,
            self._fts_state._fts_ckpt_metadata["current_ckpt_depth"],
            self._fts_state._fts_ckpt_metadata["best_ckpt_depth"],
            len(self._fts_state._fts_ckpt_metadata["best_ckpt_pgs"]),
            len(self._fts_state._curr_thawed_params),
            len(self._internal_optimizer_metadata[0]),
            trainer.checkpoint_callback.current_ckpt_depth,
            trainer.checkpoint_callback.best_ckpt_depth,
        )
        assert current_state == self.expected_state[state_key]
        if self.restore_best:
            assert self.restored_best_cnt == self.curr_depth
        else:
            assert self.restored_best_cnt == 0


@pytest.fixture(scope="function")
def ckpt_set(tmpdir_factory) -> Dict:
    """A fixture that generates a 'best' and 'kth' checkpoint to be used in scheduled finetuning resumption
    testing."""
    seed_everything(42)
    callbacks = [
        FinetuningScheduler(max_depth=1),
        FTSEarlyStopping(monitor="val_loss", patience=1, min_delta=0.001),
        FTSCheckpoint(monitor="val_loss", verbose=True, save_top_k=3),
    ]
    model = FinetuningSchedulerBoringModel()
    trainer = Trainer(default_root_dir=tmpdir_factory.getbasetemp(), callbacks=callbacks)
    trainer.fit(model)
    return {"best": trainer.checkpoint_callback.best_model_path, "kth": trainer.checkpoint_callback.kth_best_model_path}


@pytest.fixture(scope="function")
def boring_ft_schedule(tmpdir_factory) -> Tuple[Path, Dict]:
    """Generates a default finetuning schedule for 'implicit' testing, a modified one for 'explicit' mode and an
    epoch-driven transitions only one for epoch_transitions_only testing."""
    seed_everything(42)
    callbacks = [FinetuningScheduler(gen_ft_sched_only=True)]
    model = FinetuningSchedulerBoringModel()
    tmpdir = tmpdir_factory.getbasetemp()
    trainer = Trainer(default_root_dir=tmpdir, callbacks=callbacks)
    unmod_schedule_file = tmpdir / "lightning_logs" / "version_0" / f"{model.__class__.__name__}_ft_schedule.yaml"
    with pytest.raises(SystemExit):
        trainer.fit(model)
    mod_sched_dict = get_fts(trainer).load_yaml_schedule(unmod_schedule_file)
    mod_sched_dict[0]["params"].extend(mod_sched_dict.pop(1)["params"])
    mod_sched_dict[0]["max_transition_epoch"] = 3
    mod_sched_dict[1] = mod_sched_dict.pop(2)
    mod_sched_dict[1]["lr"] = 1e-06
    mod_sched_dict[2] = mod_sched_dict.pop(3)
    mod_sched_dict[2]["params"] = ["layer.0.*"]
    epoch_only_sched = deepcopy(mod_sched_dict)
    epoch_only_sched[1]["max_transition_epoch"] = 2
    epoch_only_sched[2]["max_transition_epoch"] = 2
    return unmod_schedule_file, mod_sched_dict, epoch_only_sched


@pytest.fixture(scope="function")
def invalid_schedules(tmpdir_factory) -> Dict:
    """A fixture that generates a dictionary of invalid schedules for testing."""
    valid_sched_start = """
0:
  params:
  - layer.2.bias
  - layer.2.weight"""
    valid_sched_end = """
2:
  params:
  - layer.0.bias
  - layer.0.weight"""
    non_disjoint = """
1:
  params:
  - layer.1.bias
  - layer.2.weight"""
    missing_param = """
1:
  params:
  - layer.1.bias
  - layer.missing.weight"""
    non_integer_phase = """
1.1:
  params:
  - layer.1.bias
  - layer.1.weight"""
    invalid_lr = """
1:
  params:
  - layer.1.bias
  - layer.1.weight
  lr: not_a_number"""
    lr_phase0 = """
0:
  params:
  - layer.2.bias
  - layer.2.weight
  lr: 1e-03"""
    invalid_sched = {}
    invalid_sched["missing_param"] = valid_sched_start + missing_param + valid_sched_end
    invalid_sched["non_integer"] = valid_sched_start + non_integer_phase + valid_sched_end
    invalid_sched["non_contiguous"] = valid_sched_start + valid_sched_end
    invalid_sched["non_disjoint"] = valid_sched_start + non_disjoint + valid_sched_end
    invalid_sched["dup_key"] = valid_sched_start + valid_sched_start + non_integer_phase
    invalid_sched["lr_phase0"] = lr_phase0
    invalid_sched["invalid_lr"] = valid_sched_start + invalid_lr + valid_sched_end
    tmpdir = Path(tmpdir_factory.getbasetemp())
    for k, v in invalid_sched.items():
        ft_schedule_yaml = tmpdir / f"{k}.yaml"
        fs = get_filesystem(ft_schedule_yaml)
        with fs.open(ft_schedule_yaml, "w", newline="") as fp:
            fp.write(v)
        invalid_sched[k] = ft_schedule_yaml
    return invalid_sched


class ComplexNestedModel(LightningModule):
    """A nested model with a parent (non-leaf) module parameter to validate scheduled finetuning with such
    architectures."""

    def __init__(self):
        super().__init__()
        self.test = nn.Sequential(
            OrderedDict(
                [("encoder", nn.Sequential(ConvBlockParam(3, 64), ConvBlock(64, 128))), ("decoder", ConvBlock(128, 10))]
            )
        )

    def forward(self, x):
        return self.test(x)

    def configure_optimizers(self):
        optimizer = torch.optim.SGD(self.layer.parameters(), lr=0.1)
        lr_scheduler = torch.optim.lr_scheduler.StepLR(optimizer, step_size=1, gamma=0.7)
        return [optimizer], [lr_scheduler]

    def training_step(self):
        pass

    def train_dataloader(self):
        pass


@pytest.mark.parametrize(
    "model, dist_mode, expected",
    [
        pytest.param(
            FinetuningSchedulerBoringModel(),
            True,
            (4, ["layer.2.bias", "layer.2.weight"], ["layer.0.bias", "layer.0.weight"]),
            marks=RunIf(min_gpus=2),
        ),
        (
            FinetuningSchedulerBoringModel(),
            False,
            (4, ["layer.2.bias", "layer.2.weight"], ["layer.0.bias", "layer.0.weight"]),
        ),
        (ParityModuleRNN(), False, (3, ["rnn.bias_hh_l0", "rnn.bias_ih_l0"], ["rnn.weight_hh_l0", "rnn.weight_ih_l0"])),
        (
            ComplexNestedModel(),
            False,
            (7, ["test.decoder.conv.bias", "test.decoder.conv.weight"], ["test.encoder.0.parent_param"]),
        ),
    ],
    ids=["dist_boring", "Boring", "ParityRNN", "ComplexNested"],
)
def test_gen_ft_schedule(tmpdir, model: "LightningModule", dist_mode: bool, expected: Tuple):
    """Validate the default finetuning schedule generation."""
    seed_everything(42)
    callbacks = [FinetuningScheduler(gen_ft_sched_only=True)]
    trainer_opts = {"default_root_dir": tmpdir, "callbacks": callbacks}
    if dist_mode:
        trainer_opts["strategy"] = "ddp"
        trainer_opts["gpus"] = 2
    trainer = Trainer(**trainer_opts)
    ft_schedule = tmpdir / "lightning_logs" / "version_0" / f"{model.__class__.__name__}_ft_schedule.yaml"
    with pytest.raises(SystemExit):
        trainer.fit(model)
    seed_everything(42)
    if trainer.is_global_zero:
        assert os.path.isfile(ft_schedule)
        with open(ft_schedule) as f:
            test_schedule = yaml.safe_load(f.read())
        assert isinstance(test_schedule, Dict)
        assert len(test_schedule) == expected[0]
        assert test_schedule[1]["params"] == expected[1]
        assert test_schedule[next(reversed(list(test_schedule.keys())))]["params"] == expected[2]


EXPECTED_EXPIMP_RESULTS = {
    (True, -1): (5, 0, 2, 6, 8, 3, 3, (0.001, 1e-06, 1e-05)),
    (False, -1): (7, 0, 3, 8, 8, 4, 4, (0.001, 1e-05, 1e-05, 1e-05)),
    (True, 0): (4, 0, 0, 5, 4, 1, 1, (0.001,)),
    (False, 0): (4, 0, 0, 5, 2, 1, 1, (0.001,)),
    (True, 2): (5, 0, 2, 6, 8, 3, 3, (0.001, 1e-06, 1e-05)),
    (False, 2): (6, 0, 2, 7, 6, 3, 3, (0.001, 1e-05, 1e-05)),
    (True, 999): (5, 0, 2, 6, 8, 3, 3, (0.001, 1e-06, 1e-05)),
    (False, 999): (7, 0, 3, 8, 8, 4, 4, (0.001, 1e-05, 1e-05, 1e-05)),
}


@pytest.mark.parametrize("explicit_mode", [True, False], ids=["explicit", "implicit"])
@pytest.mark.parametrize("max_depth", [-1, 0, 2, 999], ids=["default", "maxdepth0", "maxdepth2", "maxdepth999"])
def test_finetuningscheduling_explicit_implicit(tmpdir, boring_ft_schedule, explicit_mode: bool, max_depth: int):
    """Validate scheduled finetuning works as expected in 'explicit' and 'implicit' modes in the context of various
    max_depth specifications."""
    seed_everything(42)
    ft_schedule = boring_ft_schedule[1] if explicit_mode else None
    callbacks = [
        FTSEarlyStopping(monitor="val_loss", patience=1),
        FTSCheckpoint(monitor="val_loss", verbose=True),
        FinetuningScheduler(ft_schedule=ft_schedule, max_depth=max_depth),
    ]
    model = FinetuningSchedulerBoringModel()
    trainer = Trainer(default_root_dir=tmpdir, callbacks=callbacks)
    trainer.fit(model)
    finetuningscheduler_callback = get_fts(trainer)
    expected_state = EXPECTED_EXPIMP_RESULTS[(explicit_mode, max_depth)]
    assert trainer.early_stopping_callback.stopped_epoch == expected_state[0]
    assert finetuningscheduler_callback.depth_remaining == expected_state[1]
    assert finetuningscheduler_callback.curr_depth == expected_state[2]
    assert finetuningscheduler_callback._fts_state._ft_epoch == expected_state[3]
    assert len(finetuningscheduler_callback._fts_state._curr_thawed_params) == expected_state[4]
    assert len(finetuningscheduler_callback._internal_optimizer_metadata[0]) == expected_state[5]
    assert len(trainer.optimizers[0].param_groups) == expected_state[6]
    assert tuple(pg["lr"] for pg in finetuningscheduler_callback._internal_optimizer_metadata[0]) == expected_state[7]
    for pg in range(expected_state[6]):
        assert trainer.optimizers[0].param_groups[pg]["params"][0].requires_grad
    still_frozen = [
        p
        for i, d in enumerate(finetuningscheduler_callback.ft_schedule)
        if i > finetuningscheduler_callback.max_depth
        for p in finetuningscheduler_callback.ft_schedule[d]["params"]
    ]
    assert not any([p.requires_grad for n, p in trainer.model.named_parameters() if n in still_frozen])
    assert finetuningscheduler_callback.curr_depth == finetuningscheduler_callback.max_depth
    assert finetuningscheduler_callback._fts_state._ft_epoch == trainer._fit_loop.epoch_progress.current.completed


EXPECTED_DECAY_RESULTS = {
    (True, False): (5, 0, 2, 6, 8, 3, 3, 1e-6),
    (True, True): (5, 0, 2, 6, 8, 5, 5, 0.0),
    (False, False): (7, 0, 3, 8, 8, 4, 4, 1e-6),
    (False, True): (7, 0, 3, 8, 8, 7, 7, 0.0),
}


@pytest.mark.parametrize("nodecay_mode", [False, True], ids=["alldecay", "nodecay"])
@pytest.mark.parametrize("explicit_mode", [True, False], ids=["explicit", "implicit"])
def test_finetuningscheduling_decay(tmpdir, boring_ft_schedule, explicit_mode: bool, nodecay_mode: bool):
    """Validate scheduled finetuning works as expected in 'explicit' and 'implicit' modes in the context of
    different nodecay list settings.

    Separately parameterized from :meth:`test_finetuningscheduling_explicit_implicit` to avoid
    costly increase in test volume w/ minimal benefit
    """
    seed_everything(42)
    ft_schedule = boring_ft_schedule[1] if explicit_mode else None
    no_decay = ["bias"] if nodecay_mode else None
    callbacks = [
        FTSEarlyStopping(monitor="val_loss", patience=1),
        FTSCheckpoint(monitor="val_loss", verbose=True),
        FinetuningScheduler(ft_schedule=ft_schedule, max_depth=-1),
    ]
    model = FinetuningSchedulerBoringModel(no_decay=no_decay)
    trainer = Trainer(default_root_dir=tmpdir, callbacks=callbacks)
    finetuningscheduler_callback = get_fts(trainer)
    trainer.fit(model)
    expected_state = EXPECTED_DECAY_RESULTS[(explicit_mode, nodecay_mode)]
    assert trainer.early_stopping_callback.stopped_epoch == expected_state[0]
    assert finetuningscheduler_callback.depth_remaining == expected_state[1]
    assert finetuningscheduler_callback.curr_depth == expected_state[2]
    assert finetuningscheduler_callback._fts_state._ft_epoch == expected_state[3]
    assert len(finetuningscheduler_callback._fts_state._curr_thawed_params) == expected_state[4]
    assert len(finetuningscheduler_callback._internal_optimizer_metadata[0]) == expected_state[5]
    assert len(trainer.optimizers[0].param_groups) == expected_state[6]
    for pg in range(expected_state[6]):
        assert trainer.optimizers[0].param_groups[pg]["params"][0].requires_grad
    assert trainer.optimizers[0].param_groups[2]["weight_decay"] == expected_state[7]
    still_frozen = [
        p
        for i, d in enumerate(finetuningscheduler_callback.ft_schedule)
        if i > finetuningscheduler_callback.max_depth
        for p in finetuningscheduler_callback.ft_schedule[d]["params"]
    ]
    assert not any([p.requires_grad for n, p in trainer.model.named_parameters() if n in still_frozen])
    assert finetuningscheduler_callback.curr_depth == finetuningscheduler_callback.max_depth
    assert finetuningscheduler_callback._fts_state._ft_epoch == trainer._fit_loop.epoch_progress.current.completed


EXPECTED_RESUME_RESULTS = {
    (True, False, "best", -1): (0, 0, 3),
    (True, False, "best", 1): (0, 0, 1),
    (True, False, "kth", -1): (1, 0, 3),
    (True, False, "kth", 1): (1, 0, 1),
    (True, True, "best", -1): (0, 0, 3),
    (True, True, "best", 1): (0, 0, 1),
    (True, True, "kth", -1): (1, 0, 3),
    (True, True, "kth", 1): (1, 0, 1),
    (False, False, "best", -1): (0, 0, 3),
    (False, False, "best", 1): (0, 0, 1),
    (False, False, "kth", -1): (0, 0, 3),
    (False, False, "kth", 1): (0, 0, 1),
    (False, True, "best", -1): (0, 0, 3),
    (False, True, "best", 1): (0, 0, 1),
    (False, True, "kth", -1): (1, 0, 3),
    (False, True, "kth", 1): (1, 0, 1),
}
EXPECTED_WARNS = [
    "does not have many workers",
    "GPU available but",
    "`max_epochs` was not",
    "that ended mid-epoch",
    "The dirpath has changed from",
]
EXPECTED_TRAIN_CHK_WARNS = ["could not find the monitored key", "callbacks used to create"]
EXPECTED_DIRPATH = "exists and is not empty"


@pytest.mark.parametrize("diff_dirpath,", [True, False], ids=["diffdirpath", "samedirpath"])
@pytest.mark.parametrize("train_chk_mode,", [None, True], ids=["defaultchk", "trainchk"])
@pytest.mark.parametrize("ckpt,", ["best", "kth"], ids=["best", "kth"])
@pytest.mark.parametrize("max_depth", [-1, 1], ids=["nomaxdepth", "maxdepth1"])
def test_fts_callback_resume(
    tmpdir, ckpt_set, recwarn, diff_dirpath: bool, train_chk_mode: Optional[bool], ckpt: str, max_depth: int
):
    """Validate scheduled finetuning resumption functions as expected from both 'best' and 'kth'(not-best)
    checkpoints in both train/val stage check modes with and without max_depth specified."""
    resume_warns = EXPECTED_WARNS
    dirpath = None if diff_dirpath else Path(ckpt_set["best"]).parent
    resume_callbacks = [
        FTSEarlyStopping(monitor="val_loss", patience=1, min_delta=0.001),
        FTSCheckpoint(
            monitor="val_loss", dirpath=dirpath, save_on_train_epoch_end=train_chk_mode, verbose=True, save_top_k=3
        ),
    ]
    resume_callbacks.append(FinetuningScheduler(max_depth=max_depth))

    seed_everything(42)
    model = FinetuningSchedulerBoringModel()
    trainer = Trainer(default_root_dir=tmpdir, callbacks=resume_callbacks)
    finetuningscheduler_callback = get_fts(trainer)
    trainer.fit(model, ckpt_path=ckpt_set[ckpt])
    # note if save_on_train_epoch_end is set to `None` then it will be False by default
    expected_state = EXPECTED_RESUME_RESULTS[
        (
            diff_dirpath,
            resume_callbacks[1]._save_on_train_epoch_end,
            ckpt,
            max_depth,
        )
    ]
    assert trainer.checkpoint_callback.best_ckpt_depth == expected_state[0]
    assert finetuningscheduler_callback.depth_remaining == expected_state[1]
    assert finetuningscheduler_callback.curr_depth == expected_state[2]
    assert finetuningscheduler_callback.curr_depth == finetuningscheduler_callback.max_depth
    if train_chk_mode:
        resume_warns.extend(EXPECTED_TRAIN_CHK_WARNS)
    if not diff_dirpath:
        resume_warns.append(EXPECTED_DIRPATH)
    # ensure no unexpected warnings detected
    assert all([any([re.compile(w).search(w_msg.message.args[0]) for w in resume_warns]) for w_msg in recwarn.list])


EXPECTED_INTRAFIT_STATE = {
    0: (0, 3, 0, 0, 0, 0, 2, 1, 0, 0),
    1: (0, 3, 1, 0, 0, 1, 2, 1, 0, 0),
    2: (0, 3, 2, 0, 0, 1, 2, 1, 0, 0),
    3: (0, 3, 3, 0, 0, 1, 2, 1, 0, 0),
    4: (0, 3, 4, 0, 0, 1, 2, 1, 0, 0),
    5: (1, 2, 5, 0, 0, 1, 4, 2, 0, 0),
    6: (2, 1, 6, 0, 0, 1, 6, 3, 0, 0),
    7: (3, 0, 7, 0, 0, 1, 8, 4, 0, 0),
}


@pytest.mark.parametrize("restore_best", [True, False], ids=["default", "norestorebest"])
def test_finetuningscheduling_intrafit(tmpdir, restore_best: bool):
    """Inspect scheduled finetuning state within the training process to ensure it is taking the expected path in
    both restore_best modes."""
    seed_everything(42)
    model = FinetuningSchedulerBoringModel()
    callbacks = [
        TestFinetuningScheduler(expected_state=EXPECTED_INTRAFIT_STATE, restore_best=restore_best),
        FTSEarlyStopping(monitor="val_loss", patience=1),
    ]
    trainer = Trainer(default_root_dir=tmpdir, callbacks=callbacks)
    trainer.fit(model)
    finetuningscheduler_callback = get_fts(trainer)
    assert finetuningscheduler_callback.depth_remaining == 0
    assert finetuningscheduler_callback.curr_depth == 3
    assert finetuningscheduler_callback.curr_depth == finetuningscheduler_callback.max_depth


@pytest.mark.parametrize(
    "callbacks, expected",
    [
        ([FinetuningScheduler()], ("an FTSEarlyStopping", "as FTSCheck")),
        ([FinetuningScheduler(), FTSEarlyStopping(monitor="val_loss", patience=1)], ("FTSCheckpoint. Subs")),
        ([FinetuningScheduler(), EarlyStopping(monitor="val_loss", patience=1)], ("Stopping. Sub", "Checkpoint. Sub")),
        ([FinetuningScheduler(), FTSCheckpoint(monitor="val_loss", verbose=True)], ("Adding an FTSEarlyStopping")),
    ],
    ids=["default", "nondef_es", "def_es", "nondef_ftsckpt"],
)
def test_finetuningscheduler_callback_warns(tmpdir, recwarn, callbacks: List[Callback], expected: Tuple[str]):
    """Validate :class:`~finetuning_scheduler.FinetuningScheduler` warnings that require a
    :class:`~pytorch_lighting.trainer.Trainer` to be defined are properly issued"""
    model = FinetuningSchedulerBoringModel()
    trainer = Trainer(default_root_dir=tmpdir, callbacks=callbacks)
    trainer.fit(model)
    assert all([any([re.compile(w_msg).search(w.message.args[0]) for w in recwarn.list]) for w_msg in expected])


def test_finetuningscheduling_opt_warns():
    """Validate :class:`~finetuning_scheduler.FinetuningScheduler` warnings that require only an
    :class:`~pytorch_lighting.optim.Optimizer` to be defined are properly issued."""
    fts = FinetuningScheduler()
    lm = FinetuningSchedulerBoringModel()
    opt = torch.optim.SGD(lm.parameters(), lr=1e-3)
    thawed_pl = []
    with pytest.warns(UserWarning, match="no new optimizer groups will be added"):
        fts.add_optimizer_groups(lm, opt, thawed_pl)


@pytest.mark.parametrize(
    "callbacks, expected",
    [
        ([FTSCheckpoint(monitor="val_loss", verbose=True)], "please use the standard ModelCheckpoint callback."),
        ([FTSEarlyStopping(monitor="val_loss")], "please use the standard EarlyStopping callback."),
        (
            [FinetuningScheduler(), FTSCheckpoint(monitor="val_loss", save_top_k=0)],
            "Please set save_top_k to a non-zero value",
        ),
        ([FinetuningScheduler(), FinetuningScheduler(), FTSCheckpoint(monitor="val_loss")], "multiple Finetuning"),
        ([FinetuningScheduler(), FTSCheckpoint(monitor=None)], "but has no quantity to monitor"),
        ([FinetuningScheduler(ft_schedule="/tmp/fnf")], "Could not find specified finetuning scheduling file"),
    ],
    ids=["nofts_ckpt", "nofts_es", "topk0", "multifts", "nomon", "schedfnf"],
)
def test_finetuningscheduling_misconfiguration(tmpdir, callbacks: List[Callback], expected: str):
    """Validate :class:`~finetuning_scheduler.FinetuningScheduler` misconfiguration exceptions are properly
    raised."""
    model = FinetuningSchedulerBoringModel()
    trainer = Trainer(default_root_dir=tmpdir, callbacks=callbacks)
    with pytest.raises(MisconfigurationException, match=expected):
        trainer.fit(model)
        fts = callbacks[0]
        if fts.ft_schedule:
            _ = fts.load_yaml_schedule(fts.ft_schedule)


@pytest.mark.parametrize(
    "schedule_key, expected",
    [
        ("missing_param", ("did not match any named", None)),
        ("non_disjoint", ("Phases are not disjoint", None)),
        ("dup_key", ("Duplicate key", None)),
        ("lr_phase0", ("A lr for finetuning phase 0", None)),
        ("invalid_lr", ("convertable to a float", None)),
        ("non_integer", ("non-integer keys", "layer.1.bias")),
        ("non_contiguous", ("non-contiguous or non-zero-indexed keys", "layer.0.bias")),
    ],
    ids=["missing_param", "non_disjoint", "dup_key", "lr_phase0", "invalid_lr", "non_int", "non_contig"],
)
def test_finetuningscheduling_invalid_schedules(tmpdir, invalid_schedules, schedule_key: str, expected: Tuple):
    """Validate :class:`~finetuning_scheduler.FinetuningScheduler` misconfiguration exceptions are properly
    raised."""
    callbacks = [FinetuningScheduler(ft_schedule=invalid_schedules[schedule_key])]
    model = FinetuningSchedulerBoringModel()
    trainer = Trainer(default_root_dir=tmpdir, callbacks=callbacks)
    if schedule_key == "lr_phase0":
        with pytest.warns(UserWarning, match=expected[0]):
            trainer.fit(model)
    else:
        with pytest.raises(MisconfigurationException, match=expected[0]):
            trainer.fit(model)
    if expected[1]:
        corrected_path = tmpdir / "lightning_logs" / "version_0"
        corrected_schedule = corrected_path / f"{trainer.lightning_module.__class__.__name__}_ft_schedule_valid.yaml"
        valid_dict = callbacks[0].load_yaml_schedule(corrected_schedule)
        # ensure we can load our suggested schedule and it loads as expected
        assert valid_dict[1]["params"][0] == expected[1]


@pytest.mark.parametrize(
    "strategy, gpus, plugins, ismock",
    [
        pytest.param("ddp2", None, None, True),
        pytest.param("ddp2", 1, None, False, marks=RunIf(min_gpus=1)),
        pytest.param("ddp_fully_sharded", 1, None, False, marks=RunIf(fairscale_fully_sharded=True, min_gpus=1)),
        pytest.param("horovod", None, None, False, marks=RunIf(horovod=True, min_gpus=1)),
        pytest.param("deepspeed_stage_2", 1, None, False, marks=RunIf(deepspeed=True, min_gpus=1)),
    ],
    ids=["cpu_mock", "ddp2", "ddp_fully_sharded", "horovod", "deepspeed_stage_2"],
)
def test_finetuningscheduling_distributed_compat(tmpdir, strategy, gpus, plugins, ismock):
    """Validate :class:`~finetuning_scheduler.FinetuningScheduler` misconfiguration exceptions are properly raised
    for currently unsupported plugins."""
    fts_args = {"mock_strategy_wcpu": True} if ismock else {}
    callbacks = [TestFinetuningScheduler(**fts_args)]
    model = FinetuningSchedulerBoringModel()
    trainer = Trainer(default_root_dir=tmpdir, callbacks=callbacks, strategy=strategy, gpus=gpus, plugins=plugins)
    with pytest.raises(MisconfigurationException, match="has not yet been adapted for the specified distributed"):
        trainer.fit(model)


def test_finetuningscheduling_optimizer_compat(tmpdir):
    """Validate :class:`~finetuning_scheduler.FinetuningScheduler` misconfiguration exceptions are properly raised
    for multi-optimizer configurations."""

    class MultiOptFTSBoringModel(FinetuningSchedulerBoringModel):
        def configure_optimizers(self):
            parameters = list(filter(lambda x: x.requires_grad, self.parameters()))
            optimizer0 = torch.optim.SGD(parameters, lr=1e-3)
            optimizer1 = torch.optim.SGD(parameters, lr=1e-3)
            return [optimizer0, optimizer1]

    seed_everything(42)
    model = MultiOptFTSBoringModel()
    callbacks = [FinetuningScheduler()]
    trainer = Trainer(default_root_dir=tmpdir, callbacks=callbacks)
    with pytest.raises(MisconfigurationException, match="single-optimizer configuration"):
        trainer.fit(model)


@pytest.mark.parametrize(
    "epoch_only_cfg, expected_state",
    [(True, ((0, 2, 6, 8, 3, 3), "extraneous EarlyS", "maximum phase-specified")), (False, (None, "missing a max_"))],
    ids=["eponly", "noeponly"],
)
def test_finetuningscheduling_epoch_trans_only(tmpdir, boring_ft_schedule, epoch_only_cfg: bool, expected_state: Tuple):
    """Validate scheduled finetuning works as expected in 'epoch_transitions_only' mode while raising the
    appropriate exception/warning with respect to epoch_transitions_only scheduling and early stopping
    respectively."""
    seed_everything(42)
    # use appropriately configured epoch_transitions_only schedule if epoch_only_cfg, else validate config error thrown
    ft_schedule = boring_ft_schedule[2] if epoch_only_cfg else boring_ft_schedule[1]
    model = FinetuningSchedulerBoringModel()
    callbacks = [
        FTSCheckpoint(monitor="val_loss", verbose=True),
        FinetuningScheduler(ft_schedule=ft_schedule, epoch_transitions_only=True),
        FTSEarlyStopping(monitor="val_loss", patience=1),  # including an extraneous earlystopping callback to test warn
    ]
    trainer = Trainer(default_root_dir=tmpdir, callbacks=callbacks, max_epochs=6)
    finetuningscheduler_callback = get_fts(trainer)
    if epoch_only_cfg:
        # we're testing an epoch_transitions_only schedule that should trigger the specified warning
        with pytest.warns(UserWarning) as eto_warns:
            trainer.fit(model)
        assert re.compile(expected_state[1]).search(eto_warns[0].message.args[0])
        assert re.compile(expected_state[2]).search(eto_warns[1].message.args[0])
        # for the valid epoch_only_transitions schedule, verify expected state
        assert finetuningscheduler_callback.depth_remaining == expected_state[0][0]
        assert finetuningscheduler_callback.curr_depth == expected_state[0][1]
        assert finetuningscheduler_callback._fts_state._ft_epoch == expected_state[0][2]
        assert len(finetuningscheduler_callback._fts_state._curr_thawed_params) == expected_state[0][3]
        assert len(finetuningscheduler_callback._internal_optimizer_metadata[0]) == expected_state[0][4]
        assert len(trainer.optimizers[0].param_groups) == expected_state[0][5]
        for pg in range(expected_state[0][5]):
            assert trainer.optimizers[0].param_groups[pg]["params"][0].requires_grad
        assert finetuningscheduler_callback.curr_depth == finetuningscheduler_callback.max_depth
        assert finetuningscheduler_callback._fts_state._ft_epoch == trainer._fit_loop.epoch_progress.current.completed
    else:
        with pytest.raises(MisconfigurationException, match=expected_state[1]):
            trainer.fit(model)


@pytest.mark.parametrize("stop_value", [torch.tensor(np.inf), torch.tensor(np.nan)])
def test_early_stopping_on_non_finite_monitor(tmpdir, stop_value):
    callbacks = [
        FinetuningScheduler(max_depth=0),
        FTSEarlyStopping(monitor="val_loss", check_finite=True),
    ]
    losses = [4, 3, stop_value, 2, 1]
    expected_stop_epoch = 2

    class CurrentModel(FinetuningSchedulerBoringModel):
        def validation_epoch_end(self, outputs):
            val_loss = losses[self.current_epoch]
            self.log("val_loss", val_loss)

    model = CurrentModel()
    trainer = Trainer(
        default_root_dir=tmpdir,
        callbacks=callbacks,
        limit_train_batches=0.2,
        limit_val_batches=0.2,
        max_epochs=10,
    )
    trainer.fit(model)
    assert trainer.current_epoch - 1 == expected_stop_epoch
    assert trainer.early_stopping_callback.stopped_epoch == expected_stop_epoch


@pytest.mark.parametrize(
    "stopping_threshold,divergence_theshold,losses,expected_epoch",
    [
        (None, None, [8, 4, 2, 3, 4, 5, 8, 10], 5),
        (2.9, None, [9, 8, 7, 6, 5, 6, 4, 3, 2, 1], 8),
        (None, 15.9, [9, 4, 2, 16, 32, 64], 3),
    ],
)
def test_early_stopping_thresholds(tmpdir, stopping_threshold, divergence_theshold, losses, expected_epoch):
    class CurrentModel(FinetuningSchedulerBoringModel):
        def validation_epoch_end(self, outputs):
            val_loss = losses[self.current_epoch]
            self.log("abc", val_loss)

    model = CurrentModel()
    callbacks = [
        FinetuningScheduler(max_depth=0),
        FTSEarlyStopping(
            monitor="abc", stopping_threshold=stopping_threshold, divergence_threshold=divergence_theshold
        ),
    ]
    trainer = Trainer(
        default_root_dir=tmpdir,
        callbacks=callbacks,
        limit_train_batches=0.2,
        limit_val_batches=0.2,
        max_epochs=20,
    )
    trainer.fit(model)
    assert trainer.current_epoch - 1 == expected_epoch, "early_stopping failed"


@RunIf(standalone=True, min_gpus=2)
def test_fts_multi_dp(tmpdir):
    """Validate :class:`~finetuning_scheduler.FinetuningScheduler` functions properly in a supported 'dp'
    distributed context."""
    seed_everything(42)
    model = FinetuningSchedulerBoringModel()
    callbacks = [FinetuningScheduler(), FTSEarlyStopping(monitor="val_loss", patience=1)]
    trainer = Trainer(default_root_dir=tmpdir, callbacks=callbacks, strategy="dp", gpus=2)
    finetuningscheduler_callback = get_fts(trainer)
    trainer.fit(model)
    assert finetuningscheduler_callback.depth_remaining == 0
    assert finetuningscheduler_callback.curr_depth == 3
    assert finetuningscheduler_callback.curr_depth == finetuningscheduler_callback.max_depth


@RunIf(standalone=True, min_gpus=2)
def test_fts_multi_ddp(tmpdir):
    """Validate :class:`~finetuning_scheduler.FinetuningScheduler` functions properly in a supported 'ddp'
    distributed context."""
    seed_everything(42)
    model = FinetuningSchedulerBoringModel()
    callbacks = [FinetuningScheduler(), FTSEarlyStopping(monitor="val_loss", patience=1)]
    trainer = Trainer(default_root_dir=tmpdir, callbacks=callbacks, strategy="ddp", gpus=2)
    finetuningscheduler_callback = get_fts(trainer)
    trainer.fit(model)
    assert finetuningscheduler_callback.depth_remaining == 0
    assert finetuningscheduler_callback.curr_depth == 3
    assert finetuningscheduler_callback.curr_depth == finetuningscheduler_callback.max_depth


@RunIf(standalone=True, fairscale=True, min_gpus=2)
def test_fts_multi_ddp_sharded(tmpdir):
    """Validate :class:`~finetuning_scheduler.FinetuningScheduler` functions properly in a supported 'ddp_sharded'
    distributed context."""
    seed_everything(42)
    model = FinetuningSchedulerBoringModel()
    callbacks = [FinetuningScheduler(), FTSEarlyStopping(monitor="val_loss", patience=1)]
    trainer = Trainer(default_root_dir=tmpdir, callbacks=callbacks, strategy="ddp_sharded", gpus=2)
    finetuningscheduler_callback = get_fts(trainer)
    trainer.fit(model)
    assert finetuningscheduler_callback.depth_remaining == 0
    assert finetuningscheduler_callback.curr_depth == 3
    assert finetuningscheduler_callback.curr_depth == finetuningscheduler_callback.max_depth


@RunIf(standalone=True, min_gpus=2)
def test_fts_multi_ddp_spawn(tmpdir):
    """Validate :class:`~finetuning_scheduler.FinetuningScheduler` functions properly in a supported 'ddp_spawn'
    distributed context."""
    seed_everything(42)
    model = FinetuningSchedulerBoringModel()
    callbacks = [FinetuningScheduler(), FTSEarlyStopping(monitor="val_loss", patience=1)]
    trainer = Trainer(default_root_dir=tmpdir, callbacks=callbacks, strategy="ddp_spawn", gpus=2)
    trainer.fit(model)
    assert trainer.callback_metrics["val_loss"] < 0.1


@RunIf(standalone=True, min_gpus=2)
def test_fts_multi_ddp_sharded_spawn(tmpdir):
    """Validate :class:`~finetuning_scheduler.FinetuningScheduler` functions properly in a supported
    'ddp_sharded_spawn' distributed context."""
    seed_everything(42)
    model = FinetuningSchedulerBoringModel()
    callbacks = [FinetuningScheduler(), FTSEarlyStopping(monitor="val_loss", patience=1)]
    trainer = Trainer(default_root_dir=tmpdir, callbacks=callbacks, strategy="ddp_sharded_spawn", gpus=2)
    trainer.fit(model)
    assert trainer.callback_metrics["val_loss"] < 0.1
