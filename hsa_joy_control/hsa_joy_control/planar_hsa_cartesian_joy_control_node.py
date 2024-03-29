from jax import Array, jit, vmap
import jax.numpy as jnp
import rclpy
from rclpy.node import Node

from geometry_msgs.msg import Pose2D
from sensor_msgs.msg import Joy

from hsa_control_interfaces.msg import PlanarSetpoint


class PlanarHsaCartesianJoyControlNode(Node):
    def __init__(self):
        super().__init__("planar_hsa_cartesian_joy_control_node")

        # change of position in cartesian space at each time step in unit [m]
        self.declare_parameter("cartesian_delta", 1e-4)  # 0.1 mm
        self.cartesian_delta = self.get_parameter("cartesian_delta").value

        # publisher of attractors planned by the joy / user
        self.declare_parameter("attractor_topic", "attractor")
        self.attractor_pub = self.create_publisher(
            PlanarSetpoint, self.get_parameter("attractor_topic").value, 10
        )

        # if the robot is platform-down, the coordinates are inverted and with that we also need to invert the joy signals
        self.declare_parameter("invert_joy_signals", True)
        self.invert_joy_signals = self.get_parameter("invert_joy_signals").value

        # intial attractor position
        self.declare_parameter("pee_y0", 0.0)  # [m]
        # end-effector position desired by the joy / user
        self.pee_wp = jnp.array([0.0, self.get_parameter("pee_y0").value])

        self.declare_parameter("joy_signal_topic", "joy_signal")
        self.joy_signal_sub = self.create_subscription(
            Joy,
            self.get_parameter("joy_signal_topic").value,
            self.joy_signal_callback,
            10,
        )

    def joy_signal_callback(self, msg: Joy):
        joy_signal = jnp.array(msg.axes)
        self.get_logger().info(f"Received joy signal: {joy_signal}")

        # compute the position of the next attractor
        if self.invert_joy_signals:
            self.pee_wp = self.pee_wp - self.cartesian_delta * joy_signal
        else:
            self.pee_wp = self.pee_wp + self.cartesian_delta * joy_signal

        # publish attractor
        msg = PlanarSetpoint()
        # we don't specify the orientation of the end-effector
        # so we just set a dummy value
        msg.chiee_des = Pose2D(
            x=self.pee_wp[0].item(), y=self.pee_wp[1].item(), theta=0.0
        )
        self.attractor_pub.publish(msg)


def main(args=None):
    rclpy.init(args=args)
    print("Hi from planar_hsa_cartesian_joy_control_node.")

    node = PlanarHsaCartesianJoyControlNode()

    rclpy.spin(node)

    # Destroy the node explicitly
    # (optional - otherwise it will be done automatically
    # when the garbage collector destroys the node object)
    node.destroy_node()
    rclpy.shutdown()


if __name__ == "__main__":
    main()
