import cv2  # importing cv2
from functools import partial
from jax import Array, jit, vmap
import jax.numpy as jnp
import numpy as onp
from os import PathLike
from typing import Callable, Dict

from hsa_planar_control.operational_workspace import get_operational_workspace_boundaries


def robot_rendering_factory(
    forward_kinematics_end_effector_fn: Callable,
    forward_kinematics_virtual_backbone_fn: Callable,
    forward_kinematics_rod_fn: Callable,
    forward_kinematics_platform_fn: Callable,
    hsa_material: str,
    params: Dict[str, Array],
    width: int,
    height: int,
    num_points: int = 25,
    inverted_coordinates: bool = False,
    invert_colors: bool = False,
    draw_operational_workspace: bool = False,
) -> Callable:
    """
    Factory function for rendering the robot.
    Args:
        forward_kinematics_end_effector_fn: function for computing the forward kinematics of the end-effector
        forward_kinematics_virtual_backbone_fn: function for computing the forward kinematics of the virtual backbone
        forward_kinematics_rod_fn: function for computing the forward kinematics of the rods
        forward_kinematics_platform_fn: function for computing the forward kinematics of the platforms
        hsa_material: material of the HSA robot
        params: dictionary of parameters
        width: width of the image
        height: height of the image
        num_points: number of points along the length of the robot
        inverted_coordinates: whether the HSA robot is oriented tip-down (so with inverted xy coordinates)
        invert_colors: if true, invert the colors. For example make the background black instead of white etc.
        draw_operational_workspace: if true, draw the kinematic workspace of the robot
    Returns:
        draw_robot_fn: function for drawing the robot as a function of the configuration
    """
    num_segments = params["l"].shape[0]

    # plotting in OpenCV
    h, w = height, width  # img height and width
    ppm = h / (
        2.0 * jnp.sum(params["lpc"] + params["l"] + params["ldc"])
    )  # pixel per meter

    if invert_colors:
        background_color = (0, 0, 0)  # black in BGR
        base_color = (255, 255, 255)  # white color in BGR
        backbone_color = (255, 0, 0)  # blue robot color in BGR
        rod_color = (255, 0, 0)  # blue color in BGR
        platform_color = (255, 0, 0)  # blue color in BGR
        end_effector_color = (255, 255, 255)  # white color in BGR
        setpoint_color = (0, 0, 255)  # red color in BGR
        attractor_color = (82, 128, 3)  # dark green color in BGR
        ws_background_color = (70, 70, 70)  # dark gray color in BGR
        ws_boundary_color = (255, 255, 255)  # white color in BGR
        active_attraction_axis_color = (255, 255, 255)
    else:
        background_color = (255, 255, 255)  # white in BGR
        base_color = (0, 0, 0)  # black base color in BGR
        backbone_color = (255, 0, 0)  # blue robot color in BGR
        rod_color = (0, 0, 0)  # black color in BGR
        platform_color = (0, 0, 0)  # black color in BGR
        end_effector_color = (255, 0, 0)  # blue color in BGR
        setpoint_color = (0, 0, 255)  # red color in BGR
        attractor_color = (0, 255, 0)  # green color in BGR
        ws_background_color = (160, 160, 160)  # light gray color in BGR
        ws_boundary_color = (0, 0, 0)  # black color in BGR
        active_attraction_axis_color = (0, 0, 0)

    batched_forward_kinematics_virtual_backbone_fn = jit(
        vmap(
            partial(forward_kinematics_virtual_backbone_fn, params),
            in_axes=(None, 0),
            out_axes=-1,
        )
    )
    batched_forward_kinematics_rod_fn = jit(
        vmap(
            partial(forward_kinematics_rod_fn, params),
            in_axes=(None, 0, None),
            out_axes=-1,
        )
    )
    batched_forward_kinematics_platform_fn = jit(
        vmap(
            partial(forward_kinematics_platform_fn, params),
            in_axes=(None, 0),
            out_axes=0,
        )
    )

    # in x-y pixel coordinates
    if inverted_coordinates:
        uv_robot_origin = onp.array([w // 2, 0.0], dtype=jnp.int32)
    else:
        uv_robot_origin = onp.array([w // 2, h], dtype=jnp.int32)

    # we use for plotting N points along the length of the robot
    s_ps = jnp.linspace(0, jnp.sum(params["l"]), num_points)

    @jit
    def chi2u(chi: Array) -> Array:
        """
        Map Cartesian coordinates to pixel coordinates.
        Args:
            chi: Cartesian poses of shape (3)

        Returns:
            uv: pixel coordinates of shape (2)
        """
        uv_off = jnp.array((chi[:2] * ppm), dtype=jnp.int32)

        if inverted_coordinates:
            # invert the u pixel coordinate
            uv_off = uv_off.at[0].set(-uv_off[0])
        else:
            # invert the v pixel coordinate
            uv_off = uv_off.at[1].set(-uv_off[1])

        # add the uv robot origin offset
        uv = uv_robot_origin + uv_off
        return uv

    batched_chi2u = jit(vmap(chi2u, in_axes=-1, out_axes=0))

    if draw_operational_workspace:
        if (params["chiee_off"][:2] == jnp.array([0.0, 0.0])).all():
            end_effector_attached = False
        else:
            end_effector_attached = True
        pee_min_ps, pee_max_ps = get_operational_workspace_boundaries(
            hsa_material=hsa_material,
            end_effector_attached=end_effector_attached,
        )
        ws_boundary_ps = jnp.concatenate(
            [pee_min_ps, jnp.flip(pee_max_ps, axis=0)], axis=0
        )


    def draw_robot_fn(
        q: Array, chiee_des: Array = None, chiee_at: Array = None, active_attraction_axis: int = -1
    ) -> onp.ndarray:
        """
        Draw the robot for a given configuration.
        Args:
            q: configuration of the robot of shape (3)
            chiee_des: desired end-effector pose of shape (3)
            chiee_at: attractor end-effector pose of shape (3)
            active_attraction_axis: the operator (or an algorithm) is currently moving the attractor along the active attractor axis
        """
        # poses along the robot of shape (3, N)
        chiv_ps = batched_forward_kinematics_virtual_backbone_fn(q, s_ps)
        # poses of virtual backbone
        chiL_ps = batched_forward_kinematics_rod_fn(q, s_ps, 0)  # poses of left rod
        chiR_ps = batched_forward_kinematics_rod_fn(q, s_ps, 1)  # poses of left rod
        # poses of the platforms
        chip_ps = batched_forward_kinematics_platform_fn(q, jnp.arange(0, num_segments))

        # initialize background
        img = onp.zeros((w, h, 3), dtype=jnp.uint8)
        img[..., 0] = background_color[0]
        img[..., 1] = background_color[1]
        img[..., 2] = background_color[2]

        # # draw base
        # cv2.rectangle(
        #     img, (0, uv_robot_origin[1]), (w, h), color=base_color, thickness=-1
        # )

        if draw_operational_workspace:
            # draw the operational workspace
            ws_boundary = onp.array(batched_chi2u(ws_boundary_ps.T))
            cv2.fillPoly(
                img, [ws_boundary], color=ws_background_color
            )
            # cv2.polylines(
            #     img, [ws_boundary], isClosed=True, color=ws_boundary_color, thickness=2
            # )

        # draw the virtual backbone
        # add the first point of the proximal cap and the last point of the distal cap
        chiv_ps = jnp.concatenate(
            [
                (chiv_ps[:, 0] - jnp.array([0.0, params["lpc"][0], 0.0])).reshape(3, 1),
                chiv_ps,
                (
                    chiv_ps[:, -1]
                    + jnp.array(
                        [
                            -jnp.sin(chiv_ps[2, -1]) * params["ldc"][-1],
                            jnp.cos(chiv_ps[2, -1]) * params["ldc"][-1],
                            chiv_ps[2, -1],
                        ]
                    )
                ).reshape(3, 1),
            ],
            axis=1,
        )
        # curve_virtual_backbone = onp.array(batched_chi2u(chiv_ps))
        # cv2.polylines(
        #     img, [curve_virtual_backbone], isClosed=False, color=backbone_color, thickness=5
        # )

        # draw the rods
        # add the first point of the proximal cap and the last point of the distal cap
        chiL_ps = jnp.concatenate(
            [
                (chiL_ps[:, 0] - jnp.array([0.0, params["lpc"][0], 0.0])).reshape(3, 1),
                chiL_ps,
                (
                    chiL_ps[:, -1]
                    + jnp.array(
                        [
                            -jnp.sin(chiL_ps[2, -1]) * params["ldc"][-1],
                            jnp.cos(chiL_ps[2, -1]) * params["ldc"][-1],
                            chiL_ps[2, -1],
                        ]
                    )
                ).reshape(3, 1),
            ],
            axis=1,
        )
        curve_rod_left = onp.array(batched_chi2u(chiL_ps))
        cv2.polylines(
            img,
            [curve_rod_left],
            isClosed=False,
            color=rod_color,
            thickness=18,
            # thickness=2*int(ppm * params["rout"].mean(axis=0)[0])
        )
        # add the first point of the proximal cap and the last point of the distal cap
        chiR_ps = jnp.concatenate(
            [
                (chiR_ps[:, 0] - jnp.array([0.0, params["lpc"][0], 0.0])).reshape(3, 1),
                chiR_ps,
                (
                    chiR_ps[:, -1]
                    + jnp.array(
                        [
                            -jnp.sin(chiR_ps[2, -1]) * params["ldc"][-1],
                            jnp.cos(chiR_ps[2, -1]) * params["ldc"][-1],
                            chiR_ps[2, -1],
                        ]
                    )
                ).reshape(3, 1),
            ],
            axis=1,
        )
        curve_rod_right = onp.array(batched_chi2u(chiR_ps))
        cv2.polylines(
            img, [curve_rod_right], isClosed=False, color=rod_color, thickness=18
        )

        # draw the platform
        for i in range(chip_ps.shape[0]):
            # iterate over the platforms
            platform_R = jnp.array(
                [
                    [jnp.cos(chip_ps[i, 2]), -jnp.sin(chip_ps[i, 2])],
                    [jnp.sin(chip_ps[i, 2]), jnp.cos(chip_ps[i, 2])],
                ]
            )  # rotation matrix for the platform
            platform_llc = chip_ps[i, :2] + platform_R @ jnp.array(
                [
                    -params["pcudim"][i, 0] / 2,  # go half the width to the left
                    -params["pcudim"][i, 1] / 2,  # go half the height down
                ]
            )  # lower left corner of the platform
            platform_ulc = chip_ps[i, :2] + platform_R @ jnp.array(
                [
                    -params["pcudim"][i, 0] / 2,  # go half the width to the left
                    +params["pcudim"][i, 1] / 2,  # go half the height down
                ]
            )  # upper left corner of the platform
            platform_urc = chip_ps[i, :2] + platform_R @ jnp.array(
                [
                    +params["pcudim"][i, 0] / 2,  # go half the width to the left
                    +params["pcudim"][i, 1] / 2,  # go half the height down
                ]
            )  # upper right corner of the platform
            platform_lrc = chip_ps[i, :2] + platform_R @ jnp.array(
                [
                    +params["pcudim"][i, 0] / 2,  # go half the width to the left
                    -params["pcudim"][i, 1] / 2,  # go half the height down
                ]
            )  # lower right corner of the platform
            platform_curve = jnp.stack(
                [platform_llc, platform_ulc, platform_urc, platform_lrc, platform_llc],
                axis=1,
            )
            # cv2.polylines(img, [onp.array(batched_chi2u(platform_curve))], isClosed=True, color=platform_color, thickness=5)
            cv2.fillPoly(
                img, [onp.array(batched_chi2u(platform_curve))], color=platform_color
            )

        if chiee_des is not None:
            # draw the setpoint / desired end-effector pose
            setpoint = chi2u(chiee_des)
            cv2.circle(
                img,
                (setpoint[0].item(), setpoint[1].item()),
                16,
                setpoint_color,
                thickness=-1,
            )

        if chiee_at is not None:
            # draw the attractor
            setpoint = chi2u(chiee_at)
            cv2.rectangle(
                img,
                pt1=(setpoint[0].item() - 13, setpoint[1].item() - 13),
                pt2=(setpoint[0].item() + 13, setpoint[1].item() + 13),
                color=attractor_color,
                thickness=-1,
            )

        if chiee_des is not None or chiee_at is not None:
            # draw the present end-effector pose
            end_effector = chi2u(forward_kinematics_end_effector_fn(params, q))
            cv2.circle(
                img,
                (end_effector[0].item(), end_effector[1].item()),
                11,
                end_effector_color,
                thickness=-1,
            )

        arrow_origin = (25, 25)
        arrow_length = 15
        arrow_kwargs = {
            "color": active_attraction_axis_color,
            "thickness": 3,
            "tipLength": 0.3
        }
        if active_attraction_axis == 0:
            # we show double arrows along the x-axis
            cv2.arrowedLine(img, arrow_origin, (arrow_origin[0] + arrow_length, arrow_origin[1]), **arrow_kwargs)
            cv2.arrowedLine(img, arrow_origin, (arrow_origin[0] - arrow_length, arrow_origin[1]), **arrow_kwargs)
        elif active_attraction_axis == 1:
            # we show double arrows along the y-axis
            cv2.arrowedLine(img, arrow_origin, (arrow_origin[0], arrow_origin[1] + arrow_length), **arrow_kwargs)
            cv2.arrowedLine(img, arrow_origin, (arrow_origin[0], arrow_origin[1] - arrow_length), **arrow_kwargs)

        return img

    return draw_robot_fn
