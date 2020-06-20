import cv2
import os
import numpy as np
import copy
import json
import time
import shutil
from queue import Queue
from glob import glob
from argparse import ArgumentParser
from statistics import mean


class Tracker:
    def __init__(self, image_size, max_patterns=10000000, max_time=0.1):
        self.init_predictions = {'Car': [], 'Pedestrian': []};
        self.predictions = [self.init_predictions]; # {'id': id, 'box2d': [x1, y1, x2, y2], 'mv': [vx, vy], 'scale': [sx, sy], 'occlusion': number_of_occlusions, 'image': image}
        self.image_size = image_size
        self.max_occ_frames = 24
        self.frame_out_thresh = 0.2
        self.box_area_thresh = 1024
        self.max_patterns = max_patterns
        self.max_time = max_time
        self.max_frame_in = {'Car': 4, 'Pedestrian': 5}
        self.cost_thresh1 = {'Car': 0.35, 'Pedestrian': 0.83}
        self.cost_thresh2 = {'Car': 0.71, 'Pedestrian': 1.44}
        self.cost_weight = {'Car': [0.5, 1.21], 'Pedestrian': [0.2, 1.09]}
        self.sim_weight = {'Car': 1.47, 'Pedestrian': 1.76}
        self.occ_weight = {'Car': 0.84, 'Pedestrian': 1.35}
        self.last_id = -1
        self.total_cost = 0

        # cost weights for hungarian matching
        self.h_max_frame_in = {'Car': 4, 'Pedestrian': 5}
        self.h_cost_weight = {'Car': [0.16, 1.41], 'Pedestrian': [0.024, 1.09]}
        self.h_sim_weight = {'Car': 1.47, 'Pedestrian': 1.76}
        self.h_occ_weight = {'Car': 0.91, 'Pedestrian': 1.5}
        self.h_frame_in_weight = {'Car': 0.37, 'Pedestrian': 0.47}


    def calculate_cost(self, box1, box2, hist1, hist2, cls='Car', match_type='full'):
        w1, h1 = box1[2]-box1[0]+1, box1[3]-box1[1]+1
        w2, h2 = box2[2]-box2[0]+1, box2[3]-box2[1]+1
        hist_score = [cv2.compareHist(hist1[c], hist2[c], cv2.HISTCMP_CORREL) for c in range(3)]
        # hist_score = mean(hist_score)
        hist_score = min(hist_score)
        cnt1 = [box1[0]+w1/2, box1[1]+h1/2]
        cnt2 = [box2[0]+w2/2, box2[1]+h2/2]
        if match_type=='full':
            alpha = abs(cnt1[0]-cnt2[0])/(w1+w2) + abs(cnt1[1]-cnt2[1])/(h1+h2)
        else:
            alpha = ((cnt1[0]-cnt2[0])/(w1+w2))**2 + ((cnt1[1]-cnt2[1])/(h1+h2))**2
        beta = (w1+w2)/(2*np.sqrt(w1*w2)) * (h1+h2)/(2*np.sqrt(h1*h2))
        if match_type=='full':
            cost = pow(alpha, self.cost_weight[cls][0]) * pow(beta, self.cost_weight[cls][1]) * pow(2, (0.5-hist_score)*self.sim_weight[cls])
        else:
            cost = pow(alpha, self.h_cost_weight[cls][0]) * pow(beta, self.h_cost_weight[cls][1]) * pow(2, (0.5-hist_score)*self.h_sim_weight[cls])
        return cost


    def hungarian_match(self, preds1, preds2, cls='Car'):
        n1 = len(preds1)
        n2 = len(preds2)
        match_costs = [[0]*n2 for _ in range(n1)]
        hist1s = [[cv2.calcHist([cv2.resize(preds1[i]['image'], (64, 64), interpolation=cv2.INTER_CUBIC)], [c], None, [64], [0, 256]) for c in range(3)] for i in range(n1)]
        hist2s = [[cv2.calcHist([cv2.resize(preds2[i]['image'], (64, 64), interpolation=cv2.INTER_CUBIC)], [c], None, [64], [0, 256]) for c in range(3)] for i in range(n2)]
        for i in range(n1):
            for j in range(n2):
                match_costs[i][j] = self.calculate_cost(preds1[i]['box2d'], preds2[j]['box2d'], hist1s[i], hist2s[j], cls, match_type='hungarian')
        best_box_map = []
        min_cost = 1e16
        for n_occ in range(max(n1-n2, 0), max(n1-n2+self.h_max_frame_in[cls]+1, min(n2, self.h_max_frame_in[cls]))):
            n_match = n1 - n_occ
            if n_match>n2 or n_match<0:
                continue
            n_frame_in = n2-n_match
            tcosts = copy.deepcopy(match_costs)
            # frame_in_costs = []
            # for i in range(n2)# :
                # x1, y1, x2, y2 = preds2[i]['box2d']
                # cx = (x1+x2) / 2
                # cy = (y1+y2) / 2
                # dx = min(cx, self.image_size[0]-cx) / self.image_size[0]
                # dy = min(cy, self.image_size[1]-cy) / self.image_size[1]
                # frame_in_costs.append((dx+dy))# * self.h_frame_in_weight[cls])
                # print(frame_in_costs)
            for i in range(n_frame_in):
                tcosts.append([self.h_frame_in_weight[cls]]*n2)
            for i in range(n_occ):
                for j in range(len(tcosts)):
                    tcosts[j].append(self.h_occ_weight[cls])
            fcosts = copy.deepcopy(tcosts)
            tcosts = np.array(tcosts)
            tcosts = (tcosts*100000).astype(np.int)

            # hungarian algorithm
            count = 0
            if len(tcosts)>0:
                tcosts -= tcosts.min(axis=1)[:, None]
                tcosts -= tcosts.min(axis=0)
                marks = np.zeros_like(tcosts)
                prev_marks = copy.deepcopy(marks)
                while not (((marks==1).sum(axis=0)==1).all() and ((marks==1).sum(axis=1)==1).all()):
                    marks = np.zeros_like(tcosts)
                    prev_tcosts = copy.deepcopy(tcosts)
                    while True:
                        while True:
                            updated1 = False
                            for i in range(tcosts.shape[0]):
                                if np.count_nonzero(np.logical_and(tcosts[i]==0, marks[i]==0))==1 and (marks[i]!=1).all():
                                    idx = np.where(np.logical_and(tcosts[i]==0, marks[i]==0))[0][0]
                                    marks[:, idx][tcosts[:, idx]==0] = -1
                                    marks[i, :][tcosts[i, :]==0] = -1
                                    marks[i, idx] = 1
                                    updated1 = True
                            for i in range(tcosts.shape[1]):
                                if np.count_nonzero(np.logical_and(tcosts[:, i]==0, marks[:, i]==0))==1 and (marks[:, i]!=1).all():
                                    idx = np.where(np.logical_and(tcosts[:, i]==0, marks[:, i]==0))[0][0]
                                    marks[idx, :][tcosts[idx, :]==0] = -1
                                    marks[:, i][tcosts[:, i]==0] = -1
                                    marks[idx, i] = 1
                                    updated1 = True
                            if not updated1:
                                break
                        updated2 = False
                        rows = [(i, np.count_nonzero(np.logical_and(tcosts[i, :]==0, marks[i, :]==0))) for i in range(tcosts.shape[0])] + [(-1, 1e16)]
                        rows = sorted(filter(lambda r: r[1]>0, rows), key=lambda r: r[1])
                        cols = [(i, np.count_nonzero(np.logical_and(tcosts[:, i]==0, marks[:, i]==0))) for i in range(tcosts.shape[1])] + [(-1, 1e16)]
                        cols = sorted(filter(lambda c: c[1]>0, cols), key=lambda c: c[1])
                        m = min(rows[0][1], cols[0][1])
                        rows = list(filter(lambda r: r[1]==m, rows))
                        cols = list(filter(lambda c: c[1]==m, cols))
                        if (rows+cols)[0][1]<1e16:
                            cands = []
                            for r in rows:
                                if r[1]==1e16:
                                    continue
                                rcs = np.where(np.logical_and(tcosts[r[0], :]==0, marks[r[0], :]==0))[0]
                                for rc in rcs:
                                    cands.append((r[0], rc, np.count_nonzero(np.logical_and(tcosts[:, rc]==0, marks[:, rc]==0))))
                            for c in cols:
                                if c[1]==1e16:
                                    continue
                                crs = np.where(np.logical_and(tcosts[:, c[0]]==0, marks[:, c[0]]==0))[0]
                                for cr in crs:
                                    cands.append((cr, c[0], np.count_nonzero(np.logical_and(tcosts[cr, :]==0, marks[cr, :]==0))))
                            cands.sort(key=lambda cand: cand[2])
                            r, c = cands[0][0], cands[0][1]
                            marks[r, :][tcosts[r, :]==0] = -1
                            marks[:, c][tcosts[:, c]==0] = -1
                            marks[r, c] = 1
                            updated2 = True
                        if not updated2:
                            break
                    row_flags = np.zeros(tcosts.shape[0])
                    col_flags = np.zeros(tcosts.shape[1])
                    row_queue = Queue()
                    col_queue = Queue()
                    for i in range(tcosts.shape[0]):
                        if np.count_nonzero(marks[i]==1)==0:
                            row_queue.put(i)
                            row_flags[i] = 1
                    while not (row_queue.empty() and col_queue.empty()):
                        while not row_queue.empty():
                            row = row_queue.get()
                            cols = np.where(np.logical_and(marks[row, :]==-1, col_flags==0))[0]
                            for col in cols:
                                col_queue.put(col)
                                col_flags[col] = 1
                        while not col_queue.empty():
                            col = col_queue.get()
                            rows = np.where(np.logical_and(marks[:, col]==1, row_flags==0))[0]
                            for row in rows:
                                row_queue.put(row)
                                row_flags[row] = 1
                    if len(tcosts[row_flags==1])>0:
                        tmp = tcosts[row_flags==1]
                        if len(tcosts[row_flags==1][np.tile(col_flags==0, (len(tmp), 1))])>0:
                            mask_min = tcosts[row_flags==1][np.tile(col_flags==0, (len(tmp), 1))].min()
                        else:
                            mask_min = 0
                    else:
                        mask_min = 0
                    if len(tcosts[row_flags==1])>0:
                        mask = np.array([[r==1 and c==0 for c in col_flags] for r in row_flags], np.bool)
                        tcosts[mask] -= mask_min
                    if len(tcosts[row_flags==0])>0:
                        mask = np.array([[r==0 and c==1 for c in col_flags] for r in row_flags], np.bool)
                        tcosts[mask] += mask_min
                    if (prev_tcosts==tcosts).all() and (prev_marks==marks).all():
                        break
                    prev_marks = copy.deepcopy(marks)

            box_map = []
            cost = 0
            indices = set(range(n2+n_occ))
            term = False
            for i in range(tcosts.shape[0]):
                tmp = np.where(marks[i]==1)[0]
                if len(tmp)==1:
                    idx = tmp[0]
                    indices.remove(idx)
                else:
                    term = True
            for i in range(tcosts.shape[0]):
                tmp = np.where(marks[i]==1)[0]
                if len(tmp)==1:
                    idx = np.where(marks[i]==1)[0][0]
                else:
                    idx = list(indices)[0]
                    indices.remove(idx)
                if i<n1:
                    box_map.append(idx if idx<n2 else -1)
                cost += fcosts[i][idx]
            if cost < min_cost:
                min_cost = cost
                best_box_map = box_map

        return best_box_map, min_cost


    def match(self, preds1, preds2, cls='Car'):
        n1 = len(preds1)
        n2 = len(preds2)
        match_costs = [[0]*n2 for _ in range(n1)]
        cands = [[] for _ in range(n1)]
        all_cands = [[] for _ in range(n1)]
        hist1s = [[cv2.calcHist([cv2.resize(preds1[i]['image'], (64, 64), interpolation=cv2.INTER_CUBIC)], [c], None, [64], [0, 256]) for c in range(3)] for i in range(n1)]
        hist2s = [[cv2.calcHist([cv2.resize(preds2[i]['image'], (64, 64), interpolation=cv2.INTER_CUBIC)], [c], None, [64], [0, 256]) for c in range(3)] for i in range(n2)]
        for i in range(n1):
            for j in range(n2):
                match_costs[i][j] = self.calculate_cost(preds1[i]['box2d'], preds2[j]['box2d'], hist1s[i], hist2s[j], cls)
                cands[i].append(j)
                all_cands[i].append(j)
        for i in range(n1):
            all_cands[i].sort(key=lambda x: match_costs[i][x])
            tmp = list(filter(lambda x: match_costs[i][x]<=self.cost_thresh1[cls], cands[i]))
            if len(tmp)>=3:
                cands[i] = tmp
                cands[i].sort(key=lambda x: match_costs[i][x])
            else:
                tmp = list(filter(lambda x: match_costs[i][x]<=self.cost_thresh2[cls], cands[i]))
                if len(tmp)>=1:
                    cands[i] = tmp
                    cands[i].sort(key=lambda x: match_costs[i][x])
                else:
                    cands[i].sort(key=lambda x: match_costs[i][x])
            cands[i] = cands[i][:max(1, 150//(n1+n2))]
        best_box_map = []
        min_cost = 1e16

        # find at least one candidate to avoid no matching
        found1 = 0
        def rec_match_find1(rem_match, idx=0, box_map=[], curr_cost=0):
            nonlocal found1
            if found1>100:
                return
            if rem_match==0:
                found1 += 1
                nonlocal min_cost
                if curr_cost<min_cost:
                    min_cost = curr_cost
                    nonlocal best_box_map
                    best_box_map = box_map + [-1]*max(0, n1-len(box_map))
                return
            cnt = 0
            for i in all_cands[idx]:
                if i in box_map:
                    continue
                rec_match_find1(rem_match-1, idx+1, box_map+[i], curr_cost+match_costs[idx][i])

        count = 0
        start_time = time.time()
        time_over = False
        def rec_match(rem_match, idx=0, box_map=[], curr_cost=0):
            nonlocal count
            nonlocal time_over
            count += 1
            if count>self.max_patterns or time_over:
                return
            if count%10000==0:
                current_time = time.time()
                if current_time-start_time>self.max_time:
                    time_over = True
            nonlocal min_cost
            if curr_cost>=min_cost:
                return
            if rem_match==0:
                if curr_cost<min_cost:
                    min_cost = curr_cost
                    nonlocal best_box_map
                    best_box_map = box_map + [-1]*max(0, n1-len(box_map))
                return
            if rem_match>=n1-idx:
                return
            rec_match(rem_match, idx+1, box_map+[-1], curr_cost)
            for i in cands[idx]:
                if i in box_map:
                    continue
                rec_match(rem_match-1, idx+1, box_map+[i], curr_cost+match_costs[idx][i])

        rec_match_find1(min(n1, n2), curr_cost=(n1-min(n1, n2))*self.occ_weight[cls]+(n2-min(n1, n2)))
        for n_occ in range(max(n1-n2, 0), max(n1-n2+self.max_frame_in[cls]+1, min(n2, self.max_frame_in[cls]))):
            n_match = n1 - n_occ
            if n_match>n2:
                continue
            # FIXME
            for n_frame_in in range(n2-n_match+1):
                n_frame_in = n2-n_match
            rec_match(n_match, curr_cost=n_occ*self.occ_weight[cls]+n_frame_in)

        if n1==0 or n2==0:
            min_cost = 0

        return best_box_map, min_cost



    def assign_ids(self, pred, image): # {'Car': [{'box2d': [x1, y1, x2, y2]}], 'Pedestrian': [{'box2d': [x1, y1, x2, y2]}]}
        pred = copy.deepcopy(pred)
        for cls, boxes in pred.items():
            if cls not in pred:
                pred[cls] = self.init_predictions[cls]
            if cls not in self.predictions[-1]:
                last_preds = self.init_predictions[cls]
            else:
                last_preds = self.predictions[-1][cls]
            adjusted_preds = []
            n_frame_out = 0
            for box in boxes:
                bb = box['box2d']
                bb[0] = max(0, bb[0])
                bb[1] = max(0, bb[1])
                bb[2] = min(self.image_size[0]-1, bb[2])
                bb[3] = min(self.image_size[1]-1, bb[3])
                bb = [min(bb[0], bb[2]), min(bb[1], bb[3]), max(bb[0], bb[2]), max(bb[1], bb[3])]
                box['image'] = image[bb[1]:bb[3]+1, bb[0]:bb[2]+1, :]
            for p in last_preds:
                box2d = p['box2d']
                mv = p['mv']
                if len(self.predictions)>=2:
                    last2_preds = self.predictions[-2][cls]
                    if p['id'] in map(lambda p2: p2['id'], last2_preds):
                        p2 = list(filter(lambda p2: p2['id']==p['id'], last2_preds))[0]
                        mv2 = p2['mv']
                        a = [mv[0]-mv2[0], mv[1]-mv2[1]]
                        if abs(mv[0])>abs(a[0])*2 and abs(mv[1])>abs(a[1])*2:
                            mv = [mv[0]+a[0], mv[1]+a[1]]
                scale = p['scale']
                cnt = [(box2d[2]+box2d[0])/2, (box2d[3]+box2d[1])/2]
                w = box2d[2]-box2d[0]+1
                h = box2d[3]-box2d[1]+1
                sw = w * scale[0]
                sh = h * scale[1]
                x1 = int(cnt[0] - sw/2 + mv[0])
                x2 = int(cnt[0] + sw/2 + mv[0])
                y1 = int(cnt[1] - sh/2 + mv[1])
                y2 = int(cnt[1] + sh/2 + mv[1])
                box2d = [max(0, x1), max(0, y1), min(self.image_size[0]-1, x2), min(self.image_size[1]-1, y2)]
                box2d = [min(box2d[0], box2d[2]), min(box2d[1], box2d[3]), max(box2d[0], box2d[2]), max(box2d[1], box2d[3])]
                area = (box2d[2]-box2d[0]+1) * (box2d[3]-box2d[1]+1)
                if area<self.box_area_thresh:
                    continue
                box2d_inside = [max(0, box2d[0]), max(0, box2d[1]), min(self.image_size[0]-1, box2d[2]), min(self.image_size[1]-1, box2d[3])]
                area_inside = (box2d_inside[2]-box2d_inside[0]+1) * (box2d_inside[3]-box2d_inside[1]+1)
                if area_inside <=area*self.frame_out_thresh:
                    n_frame_out += 1
                    continue
                adjusted_preds.append({'id': p['id'], 'box2d': box2d_inside, 'mv': p['mv'], 'scale': p['scale'], 'occlusion': p['occlusion'], 'image': p['image']})
            box_map, cost = self.hungarian_match(adjusted_preds, boxes, cls)
            self.total_cost += cost
            prev_ids = list(map(lambda p: p['id'], adjusted_preds))
            next_ids = [prev_ids[box_map.index(i)] if i in box_map else -1 for i in range(len(boxes))]
            for i in range(len(next_ids)):
                if(next_ids[i]==-1):
                    next_ids[i] = self.last_id + 1
                    self.last_id += 1
            for i in range(len(boxes)):
                if next_ids[i] in prev_ids:
                    prev_box2d = adjusted_preds[prev_ids.index(next_ids[i])]['box2d']
                    box2d = pred[cls][i]['box2d']
                    prev_cnt = [(prev_box2d[0]+prev_box2d[2])//2, (prev_box2d[1]+prev_box2d[3])//2]
                    cnt = [(box2d[0]+box2d[2])//2, (box2d[1]+box2d[3])//2]
                    mv = [cnt[0]-prev_cnt[0], cnt[1]-prev_cnt[1]]
                    sx = (box2d[2]-box2d[0]+1) / (prev_box2d[2]-prev_box2d[0]+1)
                    sy = (box2d[3]-box2d[1]+1) / (prev_box2d[3]-prev_box2d[1]+1)
                    scale = [sx, sy]
                else:
                    mv = [0, 0]
                    scale = [1, 1]
                bb = pred[cls][i]['box2d']
                pred[cls][i] = {'box2d': pred[cls][i]['box2d'], 'id': next_ids[i], 'mv': mv, 'scale': scale, 'occlusion': 0, 'image': image[bb[1]:bb[3]+1, bb[0]:bb[2]+1, :]}
            for i in range(len(box_map)):
                if box_map[i]==-1 and adjusted_preds[i]['occlusion']<self.max_occ_frames:
                    bb = adjusted_preds[i]['box2d']
                    pred[cls].append({'box2d': bb, 'id': adjusted_preds[i]['id'], 'mv': adjusted_preds[i]['mv'], 'scale': adjusted_preds[i]['scale'], 'occlusion': adjusted_preds[i]['occlusion']+1, 'image': image[bb[1]:bb[3]+1, bb[0]:bb[2]+1, :]})
        self.predictions.append(pred)
        ret = copy.deepcopy(pred)
        for cls in ret.keys():
            tmp = []
            for box in ret[cls]:
                if box['occlusion']==0:
                    tmp.append({'box2d': box['box2d'], 'id': box['id']})
            ret[cls] = tmp
        return ret


if __name__ == '__main__':

    parser = ArgumentParser()
    parser.add_argument("-i", "--input_pred", type=str, dest="input_pred_path", required=True, help="input prediction directory path")
    parser.add_argument("-v", "--input_video", type=str, dest="input_video_path", required=True, help="input video directory path")
    parser.add_argument("-o", "--output", type=str, dest="output_path", required=True, help="output file path")
    args = parser.parse_args()

    video_total = {'Car': 0, 'Pedestrian': 0}
    video_error = {'Car': 0, 'Pedestrian': 0}

    colors = [
        (255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0), (0, 255, 255), (255, 0, 255), (128, 0, 0), (0, 128, 0), (0, 0, 128), (128, 128, 0),
        (0, 128, 128), (128, 0, 128), (255, 128, 0), (255, 0, 128), (255, 128, 128), (128, 255, 0), (0, 255, 128), (128, 255, 128), (128, 0, 255), (0, 128, 255),
        (128, 128, 255), (128, 128, 128), (0, 0, 0), (255, 255, 255),
    ]

    if not os.path.exists('debug'):
        os.mkdir('debug')

    for nv, pred in enumerate(sorted(glob(os.path.join(args.input_pred_path, '*')))):
        max_time = 0
        with open(pred) as f:
            ground_truths = json.load(f)
        ground_truths = ground_truths['sequence']
        ground_truths = list(map(lambda x: {'Car': x['Car'] if 'Car' in x.keys() else [], 'Pedestrian': x['Pedestrian'] if 'Pedestrian' in x.keys() else []}, ground_truths))
        video_name = os.path.basename(pred)
        video = os.path.join(args.input_video_path, video_name.split('.')[0]+'.mp4')
        video = cv2.VideoCapture(video)
        tracker = Tracker((1936, 1216))
        total = {'Car': 0, 'Pedestrian': 0}
        sw = {'Car': 0, 'Pedestrian': 0}
        tp = {'Car': 0, 'Pedestrian': 0}
        if os.path.exists(os.path.join('debug', video_name.split('.')[0])):
            shutil.rmtree(os.path.join('debug', video_name.split('.')[0]))
        os.mkdir(os.path.join('debug', video_name.split('.')[0]))
        for frame in range(len(ground_truths)):
            if frame%100==0:
                print(f'"{video_name}" Frame {frame+1}: ', end='')
            _, image = video.read()
            ground_truth = ground_truths[frame]
            prediction = copy.deepcopy(ground_truth)
            t1 = time.time()
            prediction = tracker.assign_ids(prediction, image)
            if frame==0:
                prev_id_map = {'Car': {}, 'Pedestrian': {}}
                for cls, gt in ground_truth.items():
                    for g in gt:
                        gt_id = g['id']
                        gt_bb = g['box2d']
                        if (gt_bb[2]-gt_bb[0]+1)*(gt_bb[3]-gt_bb[1]+1)<1024:
                            continue
                        m_id = -1
                        for p in prediction[cls]:
                            p_id = p['id']
                            p_bb = p['box2d']
                            if gt_bb==p_bb:
                                m_id = p_id
                                break
                        prev_id_map[cls][gt_id] = m_id
                prev_image = image
            else:
                debug_image1 = prev_image.copy()
                debug_image2 = image.copy()
                debug_idx = 0
                id_map = {'Car': {}, 'Pedestrian': {}}
                for cls, gt in ground_truth.items():
                    bm = 0
                    for g in gt:
                        gt_id = g['id']
                        gt_bb = g['box2d']
                        if (gt_bb[2]-gt_bb[0]+1)*(gt_bb[3]-gt_bb[1]+1)<1024:
                            continue
                        total[cls] += 1
                        m_id = -1
                        for p in prediction[cls]:
                            p_id = p['id']
                            p_bb = p['box2d']
                            if gt_bb==p_bb:
                                m_id = p_id
                                bm += 1
                                break
                        id_map[cls][gt_id] = m_id
                for cls, gt in ground_truth.items():
                    for g in gt:
                        gt_id = g['id']
                        gt_bb = g['box2d']
                        if (gt_bb[2]-gt_bb[0]+1)*(gt_bb[3]-gt_bb[1]+1)<1024:
                            continue
                        if gt_id in prev_id_map[cls].keys():
                            prev_m_id = prev_id_map[cls][gt_id]
                            if gt_id in id_map[cls].keys():
                                if prev_m_id!=id_map[cls][gt_id]:
                                    debug_bb1 = list(filter(lambda p: p['id']==prev_m_id, tracker.predictions[-2][cls]))
                                    debug_bb2 = list(filter(lambda p: p['id']==prev_m_id, tracker.predictions[-1][cls]))
                                    if len(debug_bb1)>0 and len(debug_bb2)>0:
                                        debug_bb1 = debug_bb1[0]['box2d']
                                        debug_bb2 = debug_bb2[0]['box2d']
                                        debug_image1 = cv2.rectangle(debug_image1, (debug_bb1[0], debug_bb1[1]), (debug_bb1[2], debug_bb1[3]), colors[debug_idx], 3)
                                        debug_image2 = cv2.rectangle(debug_image2, (debug_bb2[0], debug_bb2[1]), (debug_bb2[2], debug_bb2[3]), colors[debug_idx], 3)
                                        debug_idx += 1
                                    sw[cls] += 1
                                else:
                                    tp[cls] += 1
                for k, v in id_map.items():
                    prev_id_map[k] = v
                debug_image = np.concatenate([debug_image1, debug_image2], axis=1)
                cv2.imwrite(os.path.join('debug', video_name.split('.')[0], f'{frame}.png'), debug_image)
                prev_image = image

            t2 = time.time()
            max_time = max(max_time, t2-t1)
            if frame%100==0:
                print(f'#Car={len(ground_truth["Car"])}, #Pedestrian={len(ground_truth["Pedestrian"])}, ', end='')
                print(f'Time={t2-t1:.8f}({max_time:.8f}@max), Cost={tracker.total_cost}')
        print(f'Overall ({video_name})')
        for cls in sw.keys():
            video_total[cls] += 1
            video_error[cls] += sw[cls]/total[cls]
            print(f'    {cls}: total={total[cls]}, sw={sw[cls]}, tp={tp[cls]}, err={sw[cls]/total[cls]:.8f}')
        print(f'    All: err={(sw["Car"]/total["Car"]+sw["Pedestrian"]/total["Pedestrian"])/2:.8f}')
    print(f'complete Result')
    print(f'    Car: {video_error["Car"]/video_total["Car"]:.8f}')
    print(f'    Pedestrian: {video_error["Pedestrian"]/video_total["Pedestrian"]:.8f}')
    print(f'    All: err={(video_error["Car"]/video_total["Car"]+video_error["Pedestrian"]/video_total["Pedestrian"])/2:.8f}')
