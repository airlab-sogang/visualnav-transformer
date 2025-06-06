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

from UniDepth.unidepth.utils.camera import Pinhole
from UniDepth.unidepth.models import UniDepthV2

# ------------------------------- CONSTANTS ----------------------------------
THIS_DIR = Path(__file__).resolve().parent
ROBOT_CONFIG_PATH = THIS_DIR / "../config/robot.yaml"
MODEL_CONFIG_PATH = THIS_DIR / "../config/models.yaml"

with open(ROBOT_CONFIG_PATH, "r") as f:
    ROBOT_CONF = yaml.safe_load(f)
MAX_V = ROBOT_CONF["max_v"]
MAX_W = ROBOT_CONF["max_w"]
RATE = ROBOT_CONF["frame_rate"]  # Hz


def _load_model(model_name: str, device: torch.device):
    with open(MODEL_CONFIG_PATH, "r") as f:
        model_paths = yaml.safe_load(f)

    model_config_path = model_paths[model_name]["config_path"]
    with open(model_config_path, "r") as f:
        model_params = yaml.safe_load(f)

    ckpt_path = model_paths[model_name]["ckpt_path"]
    if not os.path.exists(ckpt_path):
        raise FileNotFoundError(f"Model weights not found at {ckpt_path}")

    print(f"Loading model from {ckpt_path}")
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
        self.last_ctx_time = self.get_clock().now()
        self.ctx_dt = 0.25

        self.noise_scheduler = DDPMScheduler(
            num_train_timesteps=self.model_params["num_diffusion_iters"],
            beta_schedule="squaredcos_cap_v2",
            clip_sample=True,
            prediction_type="epsilon",
        )

        # State & ROS‑interfaces --------------------------------------------
        self.context_queue: Deque[np.ndarray] = deque(maxlen=self.context_size + 1)
        self.bridge = CvBridge()

        # 로봇 타입에 따른 이미지 토픽 선택
        if args.robot == "locobot":
            image_topic = "/robot1/camera/image"  # 상수에서 가져옴
            waypoint_topic = "/robot1/waypoint"
            sampled_actions_topic = "/robot1/sampled_actions"
            trajectory_viz_topic = "/robot1/trajectory_viz"
        elif args.robot == "locobot2":
            image_topic = "/robot3/camera/image"
            waypoint_topic = "/robot3/waypoint"
            sampled_actions_topic = "/robot3/sampled_actions"
            trajectory_viz_topic = "/robot3/trajectory_viz"
        elif args.robot == "robomaster":
            image_topic = "/camera/image_color"
            waypoint_topic = "/robot3/waypoint"
            sampled_actions_topic = "/robot3/sampled_actions"
            trajectory_viz_topic = "/robot3/trajectory_viz"
        elif args.robot == "turtlebot4":
            image_topic = "/robot2/oakd/rgb/preview/image_raw"
            waypoint_topic = "/robot2/waypoint"
            sampled_actions_topic = "/robot2/sampled_actions"
            trajectory_viz_topic = "/robot2/trajectory_viz"
        else:
            raise ValueError(f"Unknown robot type: {args.robot}")

        self.create_subscription(Image, image_topic, self._image_cb, 1)
        self.waypoint_pub = self.create_publisher(Float32MultiArray, waypoint_topic, 1)
        self.sampled_actions_pub = self.create_publisher(
            Float32MultiArray, sampled_actions_topic, 1
        )
        self.viz_pub = self.create_publisher(Image, trajectory_viz_topic, 1)

        self.create_timer(1.0 / RATE, self._timer_cb)
        self.get_logger().info(
            f"Exploration node initialised for {args.robot}. Waiting for images…"
        )

        self.current_waypoint = np.zeros(2)
        self.obstacle_points = None
        self.top_view_size = (400, 400)
        # self.proximity_threshold = 0.8
        # self.safety_margin = 0.17

        if args.robot == "locobot" or args.robot == "locobot2":
            self.safety_margin = 0.05
            self.proximity_threshold = 1.0
        elif args.robot == "robomaster":
            self.safety_margin = -0.1
            self.proximity_threshold = 1.3
        elif args.robot == "turtlebot4":
            self.safety_margin = 0.2
            self.proximity_threshold = 1.4
        else:
            raise ValueError(f"Unsupported robot type: {self.args.robot}")

        self.top_view_resolution = self.top_view_size[0] / self.proximity_threshold
        self.top_view_sampling_step = 5

        # 로봇 타입에 따른 이미지 크기 설정
        if args.robot == "locobot" or args.robot == "locobot2":
            self.DIM = (320, 240)
        elif args.robot == "robomaster":
            self.DIM = (640, 360)
        elif args.robot == "turtlebot4":
            self.DIM = (320, 200)

        self._init_depth_model()

        # 시작하기 전에 중요한 파라미터들 출력
        self.get_logger().info("=" * 60)
        self.get_logger().info("EXPLORATION NODE PARAMETERS (with APF)")
        self.get_logger().info("=" * 60)
        self.get_logger().info(f"Robot type: {self.args.robot}")
        self.get_logger().info(f"Image topic: {image_topic}")
        self.get_logger().info("-" * 60)
        self.get_logger().info("ROBOT CONFIGURATION:")
        self.get_logger().info(f"  - Max linear velocity: {MAX_V} m/s")
        self.get_logger().info(f"  - Max angular velocity: {MAX_W} rad/s")
        self.get_logger().info(f"  - Frame rate: {RATE} Hz")
        self.get_logger().info(f"  - Safety margin: {self.safety_margin} m")
        self.get_logger().info(f"  - Proximity threshold: {self.proximity_threshold} m")
        self.get_logger().info("-" * 60)
        self.get_logger().info("CAMERA CONFIGURATION:")
        self.get_logger().info(f"  - Image dimensions: {self.DIM}")
        self.get_logger().info(f"  - Camera matrix (K):\n{self.K}")
        self.get_logger().info("-" * 60)
        self.get_logger().info("MODEL CONFIGURATION:")
        self.get_logger().info(f"  - Model name: {self.args.model}")
        self.get_logger().info(f"  - Device: {self.device}")
        self.get_logger().info(f"  - Context size: {self.context_size}")
        self.get_logger().info(f"  - Context update interval: {self.ctx_dt} seconds")
        self.get_logger().info(f"  - Trajectory length: {self.model_params['len_traj_pred']}")
        self.get_logger().info(f"  - Diffusion iterations: {self.model_params['num_diffusion_iters']}")
        self.get_logger().info(f"  - Image size: {self.model_params['image_size']}")
        self.get_logger().info("-" * 60)
        self.get_logger().info("DEPTH MODEL CONFIGURATION:")
        self.get_logger().info(f"  - UniDepth model: UniDepthV2")
        self.get_logger().info(f"  - Pretrained weights: lpiccinelli/unidepth-v2-vits14")
        self.get_logger().info("-" * 60)
        self.get_logger().info("OBSTACLE AVOIDANCE CONFIGURATION:")
        self.get_logger().info(f"  - Top view size: {self.top_view_size}")
        self.get_logger().info(f"  - Top view resolution: {self.top_view_resolution:.2f} pixels/m")
        self.get_logger().info(f"  - Top view sampling step: {self.top_view_sampling_step} pixels")
        self.get_logger().info("-" * 60)
        self.get_logger().info("ROS TOPICS:")
        self.get_logger().info(f"  - Subscribing to: {image_topic}")
        self.get_logger().info(f"  - Publishing waypoints to: {WAYPOINT_TOPIC}")
        self.get_logger().info(f"  - Publishing sampled actions to: {SAMPLED_ACTIONS_TOPIC}")
        self.get_logger().info(f"  - Publishing visualization to: /trajectory_viz")
        self.get_logger().info("-" * 60)
        self.get_logger().info("EXECUTION PARAMETERS:")
        self.get_logger().info(f"  - Waypoint index: {self.args.waypoint}")
        self.get_logger().info(f"  - Number of samples: {self.args.num_samples}")
        self.get_logger().info("-" * 60)
        self.get_logger().info("VISUALIZATION PARAMETERS:")
        self.get_logger().info(f"  - Pixels per meter: 3.0")
        self.get_logger().info(f"  - Lateral scale: 1.0")
        self.get_logger().info(f"  - Robot symbol length: 10 pixels")
        self.get_logger().info("=" * 60)

    def _init_depth_model(self):
        if self.args.robot == "locobot" or self.args.robot == "locobot2":
            self.K = np.load("./UniDepth/assets/fisheye/fisheye_intrinsics.npy")
            self.D = np.load("./UniDepth/assets/fisheye/fisheye_distortion.npy")
            self.map1, self.map2 = cv2.fisheye.initUndistortRectifyMap(
                self.K, self.D, np.eye(3), self.K, self.DIM, cv2.CV_16SC2
            )
        elif self.args.robot == "robomaster":
            self.K = np.load("./UniDepth/assets/robomaster/intrinsics.npy")
            self.map1, self.map2 = None, None
        elif self.args.robot == "turtlebot4":
            self.K = np.load("./UniDepth/assets/turtlebot4/intrinsics.npy")
            self.map1, self.map2 = None, None
        else:
            raise ValueError(f"Unsupported robot type: {self.args.robot}")

        # self.intrinsics_torch = torch.from_numpy(self.K).unsqueeze(0).to(self.device)
        # self.camera = Pinhole(K=self.intrinsics_torch)
        self.depth_model = (
            UniDepthV2.from_pretrained("lpiccinelli/unidepth-v2-vits14")
            .to(self.device)
            .eval()
        )

    # ------------------------------------------------------------------
    # Callbacks
    # ------------------------------------------------------------------

    def _image_cb(self, msg: Image):
        now = self.get_clock().now()
        if (now - self.last_ctx_time).nanoseconds < self.ctx_dt * 1e9:
            return  # 아직 0.25 s 안 지났으면 무시
        self.context_queue.append(msg_to_pil(msg))
        self.last_ctx_time = now
        self.get_logger().info(
            f"Image added to context queue ({len(self.context_queue)})"
        )

        # depth 추론 및 장애물 저장
        cv2_img = self.bridge.imgmsg_to_cv2(msg)

        if self.args.robot == "locobot" or self.args.robot == "locobot2":
            frame = cv2.resize(cv2_img, self.DIM)
            # undistorted = cv2.remap(
            #     frame, self.map1, self.map2, interpolation=cv2.INTER_LINEAR
            # )
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        elif self.args.robot == "robomaster":
            frame = cv2_img.copy()
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        elif self.args.robot == "turtlebot4":
            frame = cv2_img.copy()
            # frame = cv2.resize(cv2_img, self.DIM)
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

        rgb_torch = (
            torch.from_numpy(rgb).permute(2, 0, 1).unsqueeze(0).float().to(self.device)
        )

        self.intrinsics_torch = torch.from_numpy(self.K).unsqueeze(0).to(self.device)
        self.camera = Pinhole(K=self.intrinsics_torch)

        with torch.no_grad():
            outputs = self.depth_model.infer(rgb_torch, self.camera)
            points = outputs["points"].squeeze().cpu().numpy()

        X, Y, Z = points[0].flatten(), points[1].flatten(), points[2].flatten()
        mask = (Z > 0) & (Z <= self.proximity_threshold) & (Y >= -0.05)
        self._update_top_view_and_obstacles(X[mask], Y[mask], Z[mask])

    def _update_top_view_and_obstacles(self, X, Y, Z_0):
        Z = np.maximum(Z_0 - self.safety_margin, 1e-3)
        img_x = np.int32(self.top_view_size[0] // 2 + X * self.top_view_resolution)
        img_y = np.int32(self.top_view_size[1] - Z * self.top_view_resolution)

        valid = (
            (img_x >= 0)
            & (img_x < self.top_view_size[0])
            & (img_y >= 0)
            & (img_y < self.top_view_size[1])
        )

        img_x = img_x[valid]
        img_y = img_y[valid]
        depth_vals = Z[valid]
        real_x = X[valid]
        real_z = Z[valid]

        sampled_obstacles = []
        for x in range(0, self.top_view_size[0], self.top_view_sampling_step):
            col_idxs = np.where(img_x == x)[0]
            if len(col_idxs) > 0:
                closest_idx = col_idxs[np.argmin(depth_vals[col_idxs])]
                sampled_obstacles.append([real_x[closest_idx], real_z[closest_idx]])

        if sampled_obstacles:
            obs_array = np.array(sampled_obstacles)
            x_local = obs_array[:, 1]
            y_local = -obs_array[:, 0]
            self.obstacle_points = np.stack([x_local, y_local], axis=1)
        else:
            self.obstacle_points = None

    def compute_repulsive_force(
        self, point: np.ndarray, obstacles: np.ndarray, influence_range=1.0
    ) -> np.ndarray:
        rep_force = np.zeros(2)
        if obstacles is None:
            return rep_force
        for obs in obstacles:
            vec = point - obs
            dist = np.linalg.norm(vec)
            if dist < 1e-6 or dist > influence_range:
                continue
            rep_force += (1.0 / dist**3) * (vec / dist)
        return rep_force

    def apply_repulsive_forces_to_trajectories(
        self, trajectories: np.ndarray
    ) -> np.ndarray:
        if self.obstacle_points is None or len(self.obstacle_points) == 0:
            return trajectories

        updated_trajs = trajectories.copy()
        for i in range(updated_trajs.shape[0]):
            max_force = np.zeros(2)
            max_magnitude = 0.0
            for j in range(updated_trajs.shape[1]):
                pt = updated_trajs[i, j] * (MAX_V / RATE)
                rep_force = self.compute_repulsive_force(pt, self.obstacle_points)
                mag = np.linalg.norm(rep_force)
                if mag > max_magnitude:
                    max_magnitude = mag
                    max_force = rep_force
            angle = np.arctan2(max_force[1], max_force[0])
            angle = np.clip(angle, -np.pi / 4, np.pi / 4)
            rotation_matrix = np.array(
                [
                    [np.cos(angle), -np.sin(angle)],
                    [np.sin(angle), np.cos(angle)],
                ]
            )
            updated_trajs[i] = (rotation_matrix @ updated_trajs[i].T).T
        return updated_trajs

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
        traj_batch = to_numpy(get_action(naction))

        # Add a flag to track if APF was applied
        is_apf_applied = (
            self.obstacle_points is not None and len(self.obstacle_points) > 0
        )

        # Apply APF if needed
        traj_batch = self.apply_repulsive_forces_to_trajectories(traj_batch)
        self._publish_action_msgs(traj_batch)

        # 3. Publish visualisation image ------------------------------------
        self._publish_viz_image(traj_batch, is_apf_applied)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _publish_action_msgs(self, traj_batch: np.ndarray):
        sampled_actions_msg = Float32MultiArray()
        sampled_actions_msg.data = [0.0] + [float(x) for x in traj_batch.flatten()]
        self.sampled_actions_pub.publish(sampled_actions_msg)

        chosen = traj_batch[0][self.args.waypoint]
        waypoint_msg = Float32MultiArray()
        waypoint_msg.data = [float(chosen[0]), float(chosen[1])]
        self.waypoint_pub.publish(waypoint_msg)

    def _publish_viz_image(self, traj_batch: np.ndarray, is_apf_applied: bool = False):
        frame = np.array(self.context_queue[-1])  # latest RGB frame
        img_h, img_w = frame.shape[:2]
        viz = frame.copy()

        cx = img_w // 2
        cy = int(img_h * 0.95)

        # 수정사항:
        pixels_per_m = 3.0
        lateral_scale = 1.0
        robot_symbol_length = 10

        cv2.line(
            viz,
            (cx - robot_symbol_length, cy),
            (cx + robot_symbol_length, cy),
            (255, 0, 0),
            2,
        )  # Blue
        cv2.line(
            viz,
            (cx, cy - robot_symbol_length),
            (cx, cy + robot_symbol_length),
            (255, 0, 0),
            2,
        )  # Blue

        # Draw each trajectory
        for i, traj in enumerate(traj_batch):
            pts = []
            # 첫 점을 로봇 위치(cx, cy)에서 시작하도록 수정
            pts.append((cx, cy))  # 시작점은 로봇의 현재 위치

            acc_x, acc_y = 0.0, 0.0
            for dx, dy in traj:
                acc_x += dx
                acc_y += dy
                # lateral_scale 적용하여 좌우로 더 넓게
                px = int(cx - acc_y * pixels_per_m * lateral_scale)  # acc_y 사용
                py = int(cy - acc_x * pixels_per_m)
                pts.append((px, py))

            if len(pts) >= 2:
                # Change colors when APF is applied
                if is_apf_applied:
                    color = (
                        (0, 0, 255) if i == 0 else (180, 0, 255)
                    )  # Blue for main, purple for others
                else:
                    color = (
                        (0, 255, 0) if i == 0 else (255, 200, 0)
                    )  # Original green and yellow
                cv2.polylines(viz, [np.array(pts, dtype=np.int32)], False, color, 2)

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
    parser.add_argument(
        "--robot",
        type=str,
        default="locobot",
        choices=["locobot", "locobot2", "robomaster", "turtlebot4"],
        help="Robot type (locobot, robomaster, turtlebot4)",
    )
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
