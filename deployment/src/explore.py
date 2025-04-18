from __future__ import annotations

import argparse
import os
from collections import deque
from pathlib import Path
from typing import Deque

import cv2
import numpy as np
import rclpy
from cv_bridge import CvBridge
from rclpy.node import Node
from sensor_msgs.msg import Image
from std_msgs.msg import Float32MultiArray
import torch
import yaml
from diffusers.schedulers.scheduling_ddpm import DDPMScheduler

from utils import msg_to_pil, to_numpy, transform_images, load_model
from vint_train.training.train_utils import get_action
from topic_names import IMAGE_TOPIC, WAYPOINT_TOPIC, SAMPLED_ACTIONS_TOPIC

# ------------------------------- CONSTANTS ----------------------------------
THIS_DIR = Path(__file__).resolve().parent
ROBOT_CONFIG_PATH = THIS_DIR / "../config/robot.yaml"
MODEL_CONFIG_PATH = THIS_DIR / "../config/models.yaml"

with open(ROBOT_CONFIG_PATH, "r") as f:
    ROBOT_CONF = yaml.safe_load(f)
MAX_V = ROBOT_CONF["max_v"]
MAX_W = ROBOT_CONF["max_w"]
RATE = ROBOT_CONF["frame_rate"]  # Hz

# Visualisation tuning -------------------------------------------------------
PIXELS_PER_M = 3.0  # ↓ smaller → shorter drawn trajectories
ORIGIN_Y_RATIO = 0.95  # 1.0 = very bottom, 0.0 = very top
# ----------------------------------------------------------------------------


def _load_model(model_name: str, device: torch.device):
    with open(MODEL_CONFIG_PATH, "r") as f:
        model_paths = yaml.safe_load(f)

    model_config_path = model_paths[model_name]["config_path"]
    with open(model_config_path, "r") as f:
        model_params = yaml.safe_load(f)

    ckpt_path = model_paths[model_name]["ckpt_path"]
    if not os.path.exists(ckpt_path):
        raise FileNotFoundError(f"Model weights not found at {ckpt_path}")

    print(f"[INFO] Loading model from {ckpt_path}")
    model = load_model(ckpt_path, model_params, device)
    return model.to(device).eval(), model_params


class ExplorationNode(Node):
    """ROS 2 node: image‑conditioned waypoint sampling + visualisation."""

    def __init__(self, args: argparse.Namespace):
        super().__init__("exploration")
        self.args = args

        # Torch / model ------------------------------------------------------
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self.get_logger().info(f"Using device: {self.device}")

        self.model, self.model_params = _load_model(args.model, self.device)
        self.context_size: int = self.model_params["context_size"]

        self.noise_scheduler = DDPMScheduler(
            num_train_timesteps=self.model_params["num_diffusion_iters"],
            beta_schedule="squaredcos_cap_v2",
            clip_sample=True,
            prediction_type="epsilon",
        )

        # State & ROS‑interfaces --------------------------------------------
        self.context_queue: Deque[np.ndarray] = deque(maxlen=self.context_size + 1)
        self.bridge = CvBridge()

        self.create_subscription(Image, IMAGE_TOPIC, self._image_cb, 1)
        self.waypoint_pub = self.create_publisher(Float32MultiArray, WAYPOINT_TOPIC, 1)
        self.sampled_actions_pub = self.create_publisher(
            Float32MultiArray, SAMPLED_ACTIONS_TOPIC, 1
        )
        self.viz_pub = self.create_publisher(Image, "trajectory_viz", 1)

        self.create_timer(1.0 / RATE, self._timer_cb)
        self.get_logger().info("Exploration node initialised. Waiting for images…")

    # ------------------------------------------------------------------
    # Callbacks
    # ------------------------------------------------------------------

    def _image_cb(self, msg: Image):
        self.context_queue.append(msg_to_pil(msg))

    def _timer_cb(self):
        if len(self.context_queue) <= self.context_size:
            return  # not enough context yet

        # 1. Prepare tensors ------------------------------------------------
        obs_imgs = transform_images(
            list(self.context_queue), self.model_params["image_size"], center_crop=False
        ).to(self.device)
        fake_goal = torch.randn(
            (1, 3, *self.model_params["image_size"]), device=self.device
        )
        mask = torch.ones(1, device=self.device, dtype=torch.long)

        with torch.no_grad():
            obs_cond = self.model(
                "vision_encoder",
                obs_img=obs_imgs,
                goal_img=fake_goal,
                input_goal_mask=mask,
            )
            rep_fn = (
                (lambda x: x.repeat(self.args.num_samples, 1))
                if obs_cond.ndim == 2
                else (lambda x: x.repeat(self.args.num_samples, 1, 1))
            )
            obs_cond = rep_fn(obs_cond)

            len_traj = self.model_params["len_traj_pred"]
            naction = torch.randn(
                (self.args.num_samples, len_traj, 2), device=self.device
            )
            self.noise_scheduler.set_timesteps(self.model_params["num_diffusion_iters"])
            for k in self.noise_scheduler.timesteps:
                noise_pred = self.model(
                    "noise_pred_net", sample=naction, timestep=k, global_cond=obs_cond
                )
                naction = self.noise_scheduler.step(noise_pred, k, naction).prev_sample

        # 2. Publish Float32MultiArray msgs ----------------------------------
        naction_np = to_numpy(get_action(naction))
        self._publish_action_msgs(naction_np)

        # 3. Publish visualisation image ------------------------------------
        self._publish_viz_image(naction_np)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _publish_action_msgs(self, traj_batch: np.ndarray):
        sampled_actions_msg = Float32MultiArray()
        sampled_actions_msg.data = [0.0] + [float(x) for x in traj_batch.flatten()]
        self.sampled_actions_pub.publish(sampled_actions_msg)

        chosen = traj_batch[0][self.args.waypoint]
        if self.model_params.get("normalize", False):
            chosen *= MAX_V / RATE
        waypoint_msg = Float32MultiArray()
        waypoint_msg.data = [float(chosen[0]), float(chosen[1])]
        self.waypoint_pub.publish(waypoint_msg)

    def _publish_viz_image(self, traj_batch: np.ndarray):
        frame = np.array(self.context_queue[-1])  # latest RGB frame
        img_h, img_w = frame.shape[:2]
        viz = frame.copy()

        cx = img_w // 2
        cy = int(img_h * ORIGIN_Y_RATIO)

        # Draw each trajectory
        for i, traj in enumerate(traj_batch):
            pts = []
            acc_x, acc_y = 0.0, 0.0
            for dx, dy in traj:
                acc_x += dx
                acc_y += dy
                px = int(cx - dy * PIXELS_PER_M)
                py = int(cy - acc_x * PIXELS_PER_M)
                pts.append((px, py))

            if len(pts) >= 2:
                color = (0, 255, 0) if i == 0 else (255, 200, 0)
                cv2.polylines(viz, [np.array(pts, dtype=np.int32)], False, color, 1)

        img_msg = self.bridge.cv2_to_imgmsg(viz, encoding="rgb8")
        img_msg.header.stamp = self.get_clock().now().to_msg()
        self.viz_pub.publish(img_msg)


# ---------------------------------------------------------------------------
# ENTRY POINT
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser("GNM‑Diffusion exploration (ROS 2)")
    parser.add_argument("--model", "-m", default="nomad")
    parser.add_argument("--waypoint", "-w", type=int, default=2)
    parser.add_argument("--num-samples", "-n", type=int, default=8)
    args = parser.parse_args()

    rclpy.init()
    node = ExplorationNode(args)
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
