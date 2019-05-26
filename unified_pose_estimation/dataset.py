"""
Some parts of this code (preprocess.py) have been borrowed from from https://github.com/guiggh/hand_pose_action
"""
import os

import torch
import trimesh
import numpy as np
from PIL import Image

from torchvision import transforms, utils
from torch.utils.data import Dataset, DataLoader


class UnifiedPoseDataset(Dataset):

    def __init__(self, mode='train', root='../data'):
        
        if mode=='train':
            self.subjects = [1]
        elif mode == 'test':
            self.subjects = []
        else:
            raise Exception("Incorrect vallue for for 'mode': {}".format(mode))
        
        self.root = root

        subject = "Subject_1"
        subject = os.path.join(root, 'Object_6D_pose_annotation_v1', subject)
        self.actions = os.listdir(subject)
        self.object_names = ['juice', 'liquid_soap', 'milk', 'salt']

        action_to_object = {
            'open_milk': 'milk',
            'close_milk': 'milk',
            'pour_milk': 'milk',
            'open_juice_bottle': 'juice',
            'close_juice_bottle': 'juice',
            'pour_juice_bottle': 'juice',
            'open_liquid_soap': 'liquid_soap',
            'close_liquid_soap': 'liquid_soap',
            'pour_liquid_soap': 'liquid_soap',
            'put_salt': 'salt'
        }

        # load meshes to memory
        object_root = os.path.join(self.root, 'Object_models')
        self.objects = self.load_objects(object_root)

        self.samples = []
        for subject in self.subjects:
            subject = "Subject_" + str(subject)
            for action in self.actions:
                sequences = len(os.listdir(os.path.join(root, 'Video_files', subject, action)))
                for sequence in range(1, sequences + 1):
                    frames = len(os.listdir(os.path.join(root, 'Video_files', subject, action, str(sequence), 'color')))
                    for frame in range(frames):
                        sample = {
                                'subject': subject,
                                'action_name': action,
                                'seq_idx': str(sequence),
                                'frame_idx': frame,
                                'object': action_to_object[action]
                        }
                        self.samples.append(sample)


    def __len__(self):
        return len(self.samples)

    def __getitem__(self, idx):

        while True:
            try:
                return self.preprocess(idx % len(self.samples))
            except Exception as e:
                print ("Couldn't get data. Reason {}. Trying next index. ".format(e))
                idx += 1

    def get_image(self, sample):
        img_path = os.path.join(self.root, 'Video_files', sample['subject'],
                            sample['action_name'], sample['seq_idx'], 'color','color_{:04d}.jpeg'.format(sample['frame_idx']))
        img = Image.open(img_path)
        img = np.asarray(img.resize((416, 416), Image.ANTIALIAS))
        img = np.transpose(img, (2, 0, 1))
        return img

    def preprocess(self, idx):
        sample = self.samples[idx]

        image = torch.from_numpy(self.get_image(sample))

        object_category = {
        'juice': 0,
        'liquid_soap': 1,
        'milk': 2,
        'salt': 3
        }

        action_category = {
            'open_milk': 0,
            'close_milk': 1,
            'pour_milk': 2,
            'open_juice_bottle': 3,
            'close_juice_bottle': 4,
            'pour_juice_bottle': 5,
            'open_liquid_soap': 6,
            'close_liquid_soap': 7,
            'pour_liquid_soap': 8,
            'put_salt': 9
        }

        skeleton_root = os.path.join(self.root, 'Hand_pose_annotation_v1')
        object_pose_root = os.path.join(self.root, 'Object_6D_pose_annotation_v1')

        self.camera_pose = np.array(
            [[0.999988496304, -0.00468848412856, 0.000982563360594, 25.7],
            [0.00469115935266, 0.999985218048, -0.00273845880292, 1.22],
            [-0.000969709653873, 0.00274303671904, 0.99999576807, 3.902], 
            [0, 0, 0, 1]])

        self.camera_intrinsics = np.array([[1395.749023, 0, 935.732544],
                                    [0, 1395.749268, 540.681030],
                                    [0, 0, 1]])

        # Object Properties
        
        object_pose = self.get_object_pose(sample, object_pose_root) 
        corners = self.objects[sample['object']]['corners'] * 1000.
        homogeneous_corners = np.concatenate([corners, np.ones([corners.shape[0], 1])], axis=1)
        corners = object_pose.dot(homogeneous_corners.T).T
        corners = self.camera_pose.dot(corners.T).T[:, :3]
        control_points = self.get_box_3d_control_points(corners)
        homogeneous_control_points = np.array(self.camera_intrinsics).dot(control_points.T).T
        box_projection = (homogeneous_control_points / homogeneous_control_points[:, 2:])[:, :2]

        del_u, del_v, del_z, cell = self.control_to_target(box_projection, control_points)

        # object pose tensor
        true_object_pose = torch.zeros(21, 3, 5, 13, 13, dtype=torch.float32)
        u, v, z = cell
        pose = np.vstack((del_u, del_v, del_z)).T
        true_object_pose[:, :, z, u, v] = torch.from_numpy(pose)
        true_object_pose = true_object_pose.view(-1, 5, 13, 13)

        # object mask
        object_mask = torch.zeros(5, 13, 13, dtype=torch.float32)
        object_mask[z, u, v] = 1

        # object class tensor
        true_object_prob = torch.zeros(5, 13, 13, dtype=torch.long)
        true_object_prob[z, u, v] = object_category[sample['object']]

        # Hand Properties
        reorder_idx = np.array([0, 1, 6, 7, 8, 2, 9, 10, 11, 3, 12, 13, 14, 4, 15, 16, 17, 5, 18, 19, 20])
        skeleton = self.get_skeleton(sample, skeleton_root)[reorder_idx]
        homogeneous_skeleton = np.concatenate([skeleton, np.ones([skeleton.shape[0], 1])], 1)
        skeleton = self.camera_pose.dot(homogeneous_skeleton.T).T[:, :3].astype(np.float32) # mm
        homogeneous_skeleton = np.array(self.camera_intrinsics).dot(skeleton.T).T
        skeleton_projection = (homogeneous_skeleton / homogeneous_skeleton[:, 2:])[:, :2]
        
        del_u, del_v, del_z, cell = self.control_to_target(skeleton_projection, skeleton)

        # hand pose tensor
        true_hand_pose = torch.zeros(21, 3, 5, 13, 13, dtype=torch.float32)
        u, v, z = cell
        pose = np.vstack((del_u, del_v, del_z)).T
        true_hand_pose[:, :, z, u, v] = torch.from_numpy(pose)
        true_hand_pose = true_hand_pose.view(-1, 5, 13, 13)

        # hand mask
        hand_mask = torch.zeros(5, 13, 13, dtype=torch.float32)
        hand_mask[z, u, v] = 1

        # hand action tensor
        true_hand_prob = torch.zeros(5, 13, 13, dtype=torch.long)
        true_hand_prob[z, u, v] = action_category[sample['action_name']]

        return image, true_hand_pose, true_hand_prob, hand_mask, true_object_pose, true_object_prob, object_mask

    def load_objects(self, obj_root):
        object_names = ['juice', 'liquid_soap', 'milk', 'salt']
        all_models = {}
        for obj_name in object_names:
            obj_path = os.path.join(obj_root, '{}_model'.format(obj_name),
                                    '{}_model.ply'.format(obj_name))
            mesh = trimesh.load(obj_path)
            corners = trimesh.bounds.corners(mesh.bounding_box.bounds)
            all_models[obj_name] = {
                'corners': corners
            }
        return all_models

    def get_skeleton(self, sample, skel_root):
        skeleton_path = os.path.join(skel_root, sample['subject'],
                                    sample['action_name'], sample['seq_idx'],
                                    'skeleton.txt')
        # print('Loading skeleton from {}'.format(skeleton_path))
        skeleton_vals = np.loadtxt(skeleton_path)
        skeleton = skeleton_vals[:, 1:].reshape(skeleton_vals.shape[0], 21,
                                                -1)[sample['frame_idx']]
        return skeleton


    def get_object_pose(self, sample, obj_root):
        seq_path = os.path.join(obj_root, sample['subject'], sample['action_name'],
                                sample['seq_idx'], 'object_pose.txt')
        with open(seq_path, 'r') as seq_f:
            raw_lines = seq_f.readlines()
        raw_line = raw_lines[sample['frame_idx']]
        line = raw_line.strip().split(' ')
        trans_matrix = np.array(line[1:]).astype(np.float32)
        trans_matrix = trans_matrix.reshape(4, 4).transpose()
        # print('Loading obj transform from {}'.format(seq_path))
        return trans_matrix

    def downsample_points(self, points, depth):

        downsample_ratio_x = 1920 / 416.
        downsample_ratio_y = 1080 / 416.
        
        x = points[0] / downsample_ratio_x
        y = points[1] / downsample_ratio_y
        z = depth / 10. # converting to centimeters

        downsampled_x = x / 32
        downsampled_y = y / 32
        downsampled_z = z / 15

        return downsampled_x, downsampled_y, downsampled_z

    def upsample_points(self, points, depth):

        downsample_ratio_x = 1920 / 416.
        downsample_ratio_y = 1080 / 416.
        
        u = points[0] * downsample_ratio_x
        v = points[1] * downsample_ratio_y
        z = depth * 10. # converting to millimeters

        return u, v, z

    def get_cell(self, root, depth):

        downsampled_x, downsampled_y, downsampled_z = self.downsample_points(root, depth)

        u = int(downsampled_x)
        v = int(downsampled_y)
        z = int(downsampled_z)

        return (u, v, z)

    def compute_offset(self, points, cell):

        points_u, points_v, points_z = points
        points_u, points_v, points_z =  self.downsample_points((points_u, points_v), points_z)
        cell_u, cell_v, cell_z = cell
        del_u = points_u - cell_u
        del_v = points_v - cell_v
        del_z = points_z - cell_z
        return del_u, del_v, del_z

    def get_box_3d_control_points(self, corners):

        # lines (0,1), (1,2), (2,3), (3,0), (4,5), (5,6), (6,7), (7,4), (0,4), (1,5), (2,6), (3,7)

        edge_01 = (corners[0] + corners[1]) / 2.
        edge_12 = (corners[1] + corners[2]) / 2.
        edge_23 = (corners[2] + corners[3]) / 2.
        edge_30 = (corners[3] + corners[0]) / 2.
        edge_45 = (corners[4] + corners[5]) / 2.
        edge_56 = (corners[5] + corners[6]) / 2.
        edge_67 = (corners[6] + corners[7]) / 2.
        edge_74 = (corners[7] + corners[4]) / 2.
        edge_04 = (corners[0] + corners[4]) / 2.
        edge_15 = (corners[1] + corners[5]) / 2.
        edge_26 = (corners[2] + corners[6]) / 2.
        edge_37 = (corners[3] + corners[7]) / 2.

        center = np.mean(corners, axis=0)

        control_points = np.vstack((center, corners,
                            edge_01, edge_12, edge_23, edge_30,
                            edge_45, edge_56, edge_67, edge_74, 
                            edge_04, edge_15, edge_26, edge_37))

        return control_points
        
        
    def control_to_target(self, projected_points, points):

        root = projected_points[0,:]
        
        cell = self.get_cell(root, points[0,2])

        points = projected_points[:,0], projected_points[:,1], points[:,2] # px, px, mm

        del_u, del_v, del_z = self.compute_offset(points, cell)
        
        return del_u, del_v, del_z, cell

    def target_to_control(self, del_u, del_v, del_z, cell):

        u, v, z = cell

        w_u = (del_u + u)
        w_v = (del_v + v)
        w_z = (del_z + z)

        w_u, w_v, w_z = self.upsample_points((w_u, w_v), w_z)

        ones = np.ones((21,1), dtype=np.float32)

        points = np.vstack((w_u * 32, w_v * 32, np.ones_like(w_u)))

        y_hat = w_z * 15 * np.linalg.inv(self.camera_intrinsics).dot(points)

        return y_hat.T
        
if __name__ == '__main__':

    train_dataset = UnifiedPoseDataset()
    image, hand_pose, action_prob, hand_mask, object_pose, object_prob, object_mask = train_dataset[0]
    print image.size()
    print hand_pose.size()
    print action_prob.size()
    print hand_mask.size()
    print object_pose.size()
    print object_prob.size()
    print object_mask.size()
    