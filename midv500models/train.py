import argparse
import os
from pathlib import Path
from typing import Dict, Any, List

import apex
import pytorch_lightning as pl
import torch
import yaml
from albumentations.core.serialization import from_dict
from iglovikov_helper_functions.config_parsing.utils import object_from_dict
from iglovikov_helper_functions.dl.pytorch.lightning import find_average
from pytorch_lightning.loggers import NeptuneLogger
from pytorch_toolbelt.losses import JaccardLoss, BinaryFocalLoss
from torch.utils.data import DataLoader

from dataloaders import SegmentationDataset
from metrics import binary_mean_iou
from utils import get_samples, load_checkpoint


def get_args():
    parser = argparse.ArgumentParser()
    arg = parser.add_argument
    arg("-c", "--config_path", type=Path, help="Path to the config.", required=True)
    arg(
        "-i",
        "--data_path",
        type=Path,
        help="Path to the masks and images.",
        required=True,
    )
    return parser.parse_args()


class SegmentDocs(pl.LightningModule):
    def __init__(self, hparams):
        super().__init__()
        self.hparams = hparams

        self.model = object_from_dict(self.hparams["model"])
        if "resume_from_checkpoint" in self.hparams:
            corrections: Dict[str, str] = {"model.": ""}

            checkpoint = load_checkpoint(
                file_path=self.hparams["resume_from_checkpoint"],
                rename_in_layers=corrections,
            )
            self.model.load_state_dict(checkpoint["state_dict"])

        if hparams["sync_bn"]:
            self.model = apex.parallel.convert_syncbn_model(self.model)

        self.losses = [
            ("jaccard", 0.1, JaccardLoss(mode="binary", from_logits=True)),
            ("focal", 0.9, BinaryFocalLoss()),
        ]

    def forward(self, batch: Dict) -> torch.Tensor:
        return self.model(batch)

    def prepare_data(self):
        self.train_samples = get_samples(
            Path(self.hparams["data_path"]) / "images",
            Path(self.hparams["data_path"]) / "masks",
        )

    def train_dataloader(self):
        train_aug = from_dict(self.hparams["train_aug"])

        result = DataLoader(
            SegmentationDataset(self.train_samples, train_aug),
            batch_size=self.hparams["train_parameters"]["batch_size"],
            num_workers=self.hparams["num_workers"],
            shuffle=True,
            pin_memory=True,
            drop_last=True,
        )
        return result

    def val_dataloader(self):
        val_aug = from_dict(self.hparams["val_aug"])

        result = DataLoader(
            SegmentationDataset(self.train_samples, val_aug),
            batch_size=self.hparams["val_parameters"]["batch_size"],
            num_workers=self.hparams["num_workers"],
            shuffle=False,
            pin_memory=True,
            drop_last=False,
        )
        return result

    def configure_optimizers(self):
        optimizer = object_from_dict(
            self.hparams["optimizer"],
            params=filter(lambda x: x.requires_grad, self.model.parameters()),
        )

        scheduler = object_from_dict(self.hparams["scheduler"], optimizer=optimizer)
        self.optimizers = [optimizer]  # skipcq: PYL-W0201

        return self.optimizers, [scheduler]

    def training_step(self, batch, batch_idx):
        features = batch["features"]
        masks = batch["masks"]

        logits = self.forward(features)

        total_loss = 0
        logs = {}
        for loss_name, weight, loss in self.losses:
            ls_mask = loss(logits, masks)
            total_loss += weight * ls_mask
            logs[f"train_mask_{loss_name}"] = ls_mask

        logs["total_loss"] = total_loss

        logs["lr"] = self._get_current_lr()

        return {"loss": total_loss, "log": logs}

    def _get_current_lr(self) -> torch.Tensor:
        lr = [x["lr"] for x in self.optimizers[0].param_groups][0]
        return torch.Tensor([lr])[0].cuda()

    def validation_step(self, batch, batch_idx):
        features = batch["features"]
        masks = batch["masks"]

        logits = self.forward(features)

        result = {}
        for loss_name, _, loss in self.losses:
            result[f"val_mask_{loss_name}"] = loss(logits, masks)

        result["val_iou"] = binary_mean_iou(logits, masks)

        return result

    def validation_epoch_end(self, outputs: List) -> Dict[str, Any]:
        avg_val_iou = find_average(outputs, "val_iou")

        logs = {
            "val_iou": avg_val_iou,
        }

        for target_type in ["mask"]:
            for loss_name, _, _ in self.losses:
                logs[f"val_{target_type}_{loss_name}"] = find_average(
                    outputs, f"val_{target_type}_{loss_name}"
                )

        logs["epoch"] = self.trainer.current_epoch

        return {"val_iou": avg_val_iou, "log": logs}


def main():
    args = get_args()

    with open(args.config_path) as f:
        hparams = yaml.load(f, Loader=yaml.SafeLoader)

    hparams["data_path"] = args.data_path

    pipeline = SegmentDocs(hparams)

    logger = NeptuneLogger(
        api_key=os.environ["NEPTUNE_API_TOKEN"],
        project_name="zackpashkin/sandbox",
        experiment_name=f"{hparams['experiment_name']}",  # Optional,
        tags=["pytorch-lightning", "mlp"],  # Optional,
        upload_source_files=[],
    )

    Path(hparams["checkpoint_callback"]["filepath"]).mkdir(exist_ok=True, parents=True)

    trainer = object_from_dict(
        hparams["trainer"],
        checkpoint_callback=object_from_dict(hparams["checkpoint_callback"]),
        logger=logger,
    )

    trainer.fit(pipeline)


if __name__ == "__main__":
    main()
