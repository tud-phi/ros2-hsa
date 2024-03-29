from functools import partial
from jax import config as jax_config

jax_config.update("jax_enable_x64", True)  # double precision
jax_config.update("jax_platform_name", "cpu")  # use CPU
from jax import Array, jit
from jax import numpy as jnp
import rclpy
from rclpy.node import Node
from pathlib import Path
from scipy.spatial.transform import Rotation as R

from geometry_msgs.msg import Pose2D
from hsa_control_interfaces.msg import Pose2DStamped
from mocap_optitrack_interfaces.msg import RigidBodyArray, PlanarCsConfiguration

import jsrm
from jsrm.parameters.hsa_params import PARAMS_FPU_CONTROL, PARAMS_EPU_CONTROL
from jsrm.systems import planar_hsa


class PlanarCsIkNode(Node):
    def __init__(self):
        super().__init__("planar_cs_ik_node")
        self.get_logger().info("Hi from planar_cs_ik_node.")

        self.declare_parameter("baseframe_rigid_bodies_topic", "baseframe_rigid_bodies")
        self.base_frame_rigid_bodies_sub = self.create_subscription(
            RigidBodyArray,
            self.get_parameter("baseframe_rigid_bodies_topic").value,
            self.listener_callback,
            10,
        )

        self.declare_parameter("mocap_platform_id", 4)
        self.mocap_platform_id = self.get_parameter("mocap_platform_id").value

        self.declare_parameter("end_effector_pose_topic", "end_effector_pose")
        self.end_effector_pose_pub = self.create_publisher(
            Pose2DStamped, self.get_parameter("end_effector_pose_topic").value, 10
        )

        self.declare_parameter("configuration_topic", "configuration")
        self.configuration_pub = self.create_publisher(
            PlanarCsConfiguration, self.get_parameter("configuration_topic").value, 10
        )

        # filepath to symbolic expressions
        sym_exp_filepath = (
            Path(jsrm.__file__).parent
            / "symbolic_expressions"
            / f"planar_hsa_ns-1_nrs-2.dill"
        )
        (_, _, _, inverse_kinematics_end_effector_fn, _, sys_helpers) = planar_hsa.factory(
            sym_exp_filepath
        )

        self.declare_parameter("hsa_material", "fpu")
        hsa_material = self.get_parameter("hsa_material").value
        if hsa_material == "fpu":
            self.params = PARAMS_FPU_CONTROL.copy()
        elif hsa_material == "epu":
            self.params = PARAMS_EPU_CONTROL.copy()
        else:
            raise ValueError(f"Unknown HSA material: {hsa_material}")

        # parameters for specifying different rest strains
        self.declare_parameter("kappa_b_eq", self.params["kappa_b_eq"].mean().item())
        self.declare_parameter("sigma_sh_eq", self.params["sigma_sh_eq"].mean().item())
        self.declare_parameter("sigma_a_eq", self.params["sigma_a_eq"].flatten().tolist())
        kappa_b_eq = self.get_parameter("kappa_b_eq").value
        sigma_sh_eq = self.get_parameter("sigma_sh_eq").value
        sigma_a_eq = self.get_parameter("sigma_a_eq").value
        self.params["kappa_b_eq"] = kappa_b_eq * jnp.ones_like(
            self.params["kappa_b_eq"]
        )
        self.params["sigma_sh_eq"] = sigma_sh_eq * jnp.ones_like(
            self.params["sigma_sh_eq"]
        )
        self.params["sigma_a_eq"] = jnp.array(sigma_a_eq).reshape(self.params["sigma_a_eq"].shape)
        # actual rest strain
        self.xi_eq = sys_helpers["rest_strains_fn"](self.params)  # rest strains
        self.get_logger().info(f"sigma_a_eq: {self.params['sigma_a_eq']}")

        # pose offset of end-effector relative to top surface of the platform
        self.declare_parameter("chiee_off", [0.0, 0.0, 0.0])
        self.params["chiee_off"] = jnp.array(self.get_parameter("chiee_off").value)

        # intialize and jit the inverse kinematics function
        self.inverse_kinematics_end_effector_fn = jit(
            partial(inverse_kinematics_end_effector_fn, self.params)
        )
        chiee_dummy = jnp.zeros((3,))
        q_dummy = self.inverse_kinematics_end_effector_fn(chiee_dummy)
        self.get_logger().info("Jitting of inverse kinematics function done")

        # self.declare_parameter("tf_base_topic", "tf_base")
        # self.declare_parameter("tf_platform_topic", "tf_platform")
        # self.subscription = self.create_subscription(
        #     TransformStamped,
        #     self.get_parameter("tf_base_topic").value,
        #     self.listener_callback,
        #     10,
        # )
        # # transformation from base tf to the start of the proximal end of the metamaterial
        # self.tf_fixed_base_offset = np.eye(4)
        # # transformation from base tf to the distal end of the metamaterial to the tf of the platform
        # self.tf_fixed_platform_offset = np.eye(4)

    def listener_callback(self, msg):
        for rigid_body_msg in msg.rigid_bodies:
            # self.get_logger().info('Rigid body: "%s"' % rigid_body_msg)
            if rigid_body_msg.id == self.mocap_platform_id:
                self.process_platform_msg(rigid_body_msg)

    def process_platform_msg(self, msg):
        position_msg = msg.pose_stamped.pose.position
        orientation_msg = msg.pose_stamped.pose.orientation

        position = jnp.array([position_msg.x, position_msg.y, position_msg.z])
        quat = jnp.array(
            [orientation_msg.x, orientation_msg.y, orientation_msg.z, orientation_msg.w]
        )
        rot = R.from_quat(quat)
        rotmat = jnp.array(rot.as_matrix())
        euler_xyz = jnp.array(rot.as_euler("xyz", degrees=False))

        # subtract distance from the platform marker to the end-effector
        # we assume that a reflective marker attached to the end-effector is chosen as the pivot point of the platform rigid body
        # this pivot point reflective marker has a length of 7mm between its centroid and the surface of the end-effector
        position = position - rotmat @ jnp.array([0, 0, 0.007])

        # define the SE(2) pose of the end-effector frame\
        # y-axis of the world frame becomes the negative x-axis of the end-effector frame
        # z-axis of the world frame becomes the y-axis of the end-effector frame
        # the rotation around the x-axis of the world frame is the same as the rotation around the negative z-axis of the end-effector frame
        chiee = jnp.array(
            [
                -position[1],
                position[2],
                -euler_xyz[0],
            ]
        )

        ee_pose_msg = Pose2D(
            x=chiee[0].item(), y=chiee[1].item(), theta=chiee[2].item()
        )
        ee_pose_stamped_msg = Pose2DStamped(header=msg.header, pose=ee_pose_msg)
        self.end_effector_pose_pub.publish(ee_pose_stamped_msg)

        # apply inverse kinematics
        q = self.inverse_kinematics_end_effector_fn(chiee)

        # publish configuration
        configuration_msg = PlanarCsConfiguration()
        configuration_msg.header = msg.header
        configuration_msg.kappa_b = q[0].item()
        configuration_msg.sigma_sh = q[1].item()
        configuration_msg.sigma_a = q[2].item()
        self.configuration_pub.publish(configuration_msg)


def main(args=None):
    rclpy.init(args=args)
    print("Hi from hsa_inverse_kinematics.")

    node = PlanarCsIkNode()

    rclpy.spin(node)

    # Destroy the node explicitly
    # (optional - otherwise it will be done automatically
    # when the garbage collector destroys the node object)
    node.destroy_node()
    rclpy.shutdown()


if __name__ == "__main__":
    main()
