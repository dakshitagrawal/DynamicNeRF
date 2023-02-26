import torch

from run_nerf_helpers import NDC2world, induce_flow


def img2mse(x, y, M=None):
    if M == None:
        return torch.mean((x - y) ** 2)
    else:
        return torch.sum((x - y) ** 2 * M) / (torch.sum(M) + 1e-8) / x.shape[-1]


def img2mae(x, y, M=None):
    if M == None:
        return torch.mean(torch.abs(x - y))
    else:
        return torch.sum(torch.abs(x - y) * M) / (torch.sum(M) + 1e-8) / x.shape[-1]


def L1(x, M=None):
    if M == None:
        return torch.mean(torch.abs(x))
    else:
        return torch.sum(torch.abs(x) * M) / (torch.sum(M) + 1e-8) / x.shape[-1]


def L2(x, M=None):
    if M == None:
        return torch.mean(x**2)
    else:
        return torch.sum((x**2) * M) / (torch.sum(M) + 1e-8) / x.shape[-1]


def entropy(x):
    return -torch.sum(x * torch.log(x + 1e-19)) / x.shape[0]


def mse2psnr(x):
    return -10.0 * torch.log(x) / torch.log(torch.Tensor([10.0]))


def loss_RGB(pred_rgb, target_rgb, loss_dict, key, mask=None):
    img_loss = img2mse(pred_rgb, target_rgb, mask)
    psnr = mse2psnr(img_loss)
    loss_dict[f"psnr{key}"] = psnr
    loss_dict[f"img{key}_loss"] = img_loss
    return loss_dict


def consistency_loss(ret, loss_dict):
    loss_dict["consistency_loss"] = L1(ret["sceneflow_f"] + ret["sceneflow_f_b"]) + L1(
        ret["sceneflow_b"] + ret["sceneflow_b_f"]
    )
    return loss_dict


def mask_loss(loss_dict, key, blending, dynamicness, mask):
    loss_dict[f"mask{key}_loss"] = L1(blending[mask[:, 0].type(torch.bool)]) + img2mae(
        dynamicness[..., None], 1 - mask
    )
    return loss_dict


def sparsity_loss(ret, loss_dict):
    loss_dict["sparse_loss"] = entropy(ret["weights_d"]) + entropy(ret["blending"])
    return loss_dict


def slow_scene_flow(ret, loss_dict):
    # Slow scene flow. The forward and backward sceneflow should be small.
    loss_dict["slow_loss"] = L1(ret["sceneflow_b"]) + L1(ret["sceneflow_f"])
    return loss_dict


def order_loss(ret, loss_dict, mask):
    loss_dict["order_loss"] = torch.mean(
        torch.square(
            ret["depth_map_d"][mask[:, 0].type(torch.bool)]
            - ret["depth_map_s"].detach()[mask[:, 0].type(torch.bool)]
        )
    )
    return loss_dict


def motion_loss(ret, loss_dict, poses, img_i, batch_grid, hwf):
    H, W, focal = tuple(hwf)
    num_img = len(poses)

    # Compuate EPE between induced flow and true flow (forward flow).
    # The last frame does not have forward flow.
    if img_i < num_img - 1:
        pts_f = ret["raw_pts_f"]
        weight = ret["weights_d"]
        pose_f = poses[img_i + 1, :3, :4]
        induced_flow_f = induce_flow(
            H, W, focal, pose_f, weight, pts_f, batch_grid[..., :2]
        )
        flow_f_loss = img2mae(induced_flow_f, batch_grid[:, 2:4], batch_grid[:, 4:5])
        loss_dict["flow_f_loss"] = flow_f_loss

    # Compuate EPE between induced flow and true flow (backward flow).
    # The first frame does not have backward flow.
    if img_i > 0:
        pts_b = ret["raw_pts_b"]
        weight = ret["weights_d"]
        pose_b = poses[img_i - 1, :3, :4]
        induced_flow_b = induce_flow(
            H, W, focal, pose_b, weight, pts_b, batch_grid[..., :2]
        )
        flow_b_loss = img2mae(induced_flow_b, batch_grid[:, 5:7], batch_grid[:, 7:8])
        loss_dict["flow_b_loss"] = flow_b_loss

    return loss_dict


def smooth_scene_flow(ret, loss_dict, hwf):
    # Smooth scene flow. The summation of the forward and backward sceneflow should be small.
    H, W, focal = tuple(hwf)
    loss_dict["smooth_loss"] = compute_sf_smooth_loss(
        ret["raw_pts"], ret["raw_pts_f"], ret["raw_pts_b"], H, W, focal
    )
    loss_dict["sf_smooth_loss"] = compute_sf_smooth_loss(
        ret["raw_pts_b"], ret["raw_pts"], ret["raw_pts_b_b"], H, W, focal
    ) + compute_sf_smooth_loss(
        ret["raw_pts_f"], ret["raw_pts_f_f"], ret["raw_pts"], H, W, focal
    )

    # Spatial smooth scene flow. (loss adapted from NSFF)
    loss_dict["sp_smooth_loss"] = compute_sf_smooth_s_loss(
        ret["raw_pts"], ret["raw_pts_f"], H, W, focal
    ) + compute_sf_smooth_s_loss(ret["raw_pts"], ret["raw_pts_b"], H, W, focal)

    return loss_dict


# Spatial smoothness (adapted from NSFF)
def compute_sf_smooth_s_loss(pts1, pts2, H, W, f):

    N_samples = pts1.shape[1]

    # NDC coordinate to world coordinate
    pts1_world = NDC2world(pts1[..., : int(N_samples * 0.95), :], H, W, f)
    pts2_world = NDC2world(pts2[..., : int(N_samples * 0.95), :], H, W, f)

    # scene flow in world coordinate
    scene_flow_world = pts1_world - pts2_world

    return L1(scene_flow_world[..., :-1, :] - scene_flow_world[..., 1:, :])


# Temporal smoothness
def compute_sf_smooth_loss(pts, pts_f, pts_b, H, W, f):

    N_samples = pts.shape[1]

    pts_world = NDC2world(pts[..., : int(N_samples * 0.9), :], H, W, f)
    pts_f_world = NDC2world(pts_f[..., : int(N_samples * 0.9), :], H, W, f)
    pts_b_world = NDC2world(pts_b[..., : int(N_samples * 0.9), :], H, W, f)

    # scene flow in world coordinate
    sceneflow_f = pts_f_world - pts_world
    sceneflow_b = pts_b_world - pts_world

    # For a 3D point, its forward and backward sceneflow should be opposite.
    return L2(sceneflow_f + sceneflow_b)


def depth_loss(dyn_depth, gt_depth):
    t_d = torch.median(dyn_depth)
    s_d = torch.mean(torch.abs(dyn_depth - t_d))
    dyn_depth_norm = (dyn_depth - t_d) / s_d

    t_gt = torch.median(gt_depth)
    s_gt = torch.mean(torch.abs(gt_depth - t_gt))
    gt_depth_norm = (gt_depth - t_gt) / s_gt

    return torch.mean((dyn_depth_norm - gt_depth_norm) ** 2)
