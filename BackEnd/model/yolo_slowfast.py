import torch
import numpy as np
import os,cv2,time,torch,random,pytorchvideo,warnings,argparse,math
warnings.filterwarnings("ignore",category=UserWarning)

from pytorchvideo.transforms.functional import (
    uniform_temporal_subsample,
    short_side_scale_with_boxes,
    clip_boxes_to_image,)
from torchvision.transforms._functional_video import normalize
from pytorchvideo.data.ava import AvaLabeledVideoFramePaths
from pytorchvideo.models.hub import slowfast_r50_detection
from deep_sort.deep_sort import DeepSort
import datetime
from collections import deque

from gluoncv.model_zoo import get_model
from gluoncv.utils.filesystem import try_import_decord
from gluoncv.data.transforms import video
from mxnet import gluon, nd, init, context
decord = try_import_decord()
from PIL import Image
import cv2
from mxnet.gluon.data.vision import transforms

import tensorflow as tf
from tensorflow.keras.preprocessing import sequence

class MyVideoCapture:
    
    def __init__(self, source):
        self.cap = cv2.VideoCapture(source)
        self.idx = -1
        self.end = False
        self.stack = []
        
    def read(self):
        self.idx += 1
        ret, img = self.cap.read()
        if ret:
            self.stack.append(img)
        else:
            self.end = True
        return ret, img
    
    def to_tensor(self, img):
        img = torch.from_numpy(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
        return img.unsqueeze(0)
        
    def get_video_clip(self):
        assert len(self.stack) > 0, "clip length must large than 0 !"
        self.stack = [self.to_tensor(img) for img in self.stack]
        clip = torch.cat(self.stack).permute(-1, 0, 1, 2)
        del self.stack
        self.stack = []
        return clip
    
    def release(self):
        self.cap.release()
        
def tensor_to_numpy(tensor):
    img = tensor.cpu().numpy().transpose((1, 2, 0))
    return img

def get_numpy_from_nonfixed_4d_array(aa, fixed_length, padding_value=0):
    rows = []
    for a_1 in aa:
        row1 = []
        for a_2 in a_1:
            for a_3 in a_2:
                
                for a_4 in a_3:
                    rows.append(np.pad(a, (0, fixed_length), 'constant', constant_values=padding_value)[:fixed_length])
    return np.concatenate(rows, axis=0).reshape(-1, fixed_length)

def clip_width_by_id(clip, boxes): #(frames, rgb, height, width)
    sec, _, H, W = clip.shape
    result = []
    boxes = np.array(boxes, dtype='int')
    for box in boxes:
        clip_box = clip[:,:,box[1]:box[3],box[0]:box[2]]
        clip_result = []
        for s in range(sec):
            clip_box_sec = np.array(clip_box[s]) # (rgb, height, width) 
            ##### 잘라내는 방법 1 : 영역만큼 잘라내서 패딩하고 크롭하기
            # if (clip_box_sec.shape[1]) and (clip_box_sec.shape[2]): # 길이가 0인 박스는 건너뛰기
            #     tmp = clip_box_sec.reshape(-1, clip_box_sec.shape[2]) # 위아래로 합치기 = (rgb * height, width)
            #     tmp = np.concatenate((tmp, np.zeros((len(tmp), W//2-tmp.shape[1]), dtype='uint8')), axis=1) # 가로방향 패딩 = (rgb * height, 960)
            #     tmp = np.array(np.split(tmp, 3, axis=0)) # RGB 분할 = (rgb, height, 960) 
            #     tmp = tmp.reshape(tmp.shape[1], -1) # 좌우로 합치기 = (height, rgb * 960)
            #     tmp = np.concatenate((tmp, np.zeros((H//2-tmp.shape[0], len(tmp[0])), dtype='uint8')), axis=0) # 세로방향 패딩 = (540, rgb * 960)
            #     tmp = np.array(np.split(tmp, 3, axis=1)) # RGB 분할 = (rgb, 540, 960) - 사진이 3등분으로 저장됨;;
            #     clip_result.append(tmp)
            # else:
            #     clip_result.append(np.zeros((3, 1920//2, 1080//2), dtype='uint8'))
            
            ##### 잘라내는 방법 2 : 영역에 대해 opencv로 224 * 224로 만들기
            resized_frame = cv2.resize(np.transpose(clip_box_sec, (1, 2, 0)), (224, 224))
            clip_result.append(np.array(resized_frame))
        result.append(clip_result)
    return np.array(result)
        

def swim_inference_transform(
    clip, 
    boxes,
    num_frames = 64, #if using slowfast_r50_detection, change this to 32, 4 for slow 
    crop_size = 1080, 
    data_mean = [0.45, 0.45, 0.45], 
    data_std = [0.225, 0.225, 0.225],
    slow_fast_alpha = 4, #if using slowfast_r50_detection, change this to 4, None for slow
):
    boxes = np.array(boxes)
    clip = np.transpose(clip, (1, 0, 2, 3))
    
    new_clip = clip_width_by_id(clip, boxes) # 방법 2 - (tid, frames, width, height, rgb)
    new_clip = np.transpose(new_clip, (1, 0, 2, 3, 4)) # (frames, tid, height, width, rgb)
    
    fast_frame_id_list = range(0, 64, 2)
    slow_frame_id_list = range(0, 64, 16)
    frame_id_list = list(fast_frame_id_list) + list(slow_frame_id_list)
    clip_input = [new_clip[vid, :, :, :, :] for vid, _ in enumerate(frame_id_list)] # (frames, tid, height, width, rgb)
    clip_input = np.transpose(clip_input, (1, 0, 2, 3, 4)) # (tid, frames, height, width, rgb)
    
    transform_fn = transforms.Compose([
        video.VideoToTensor(),
        video.VideoNormalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225])
    ])
    
    result = []
    for _, clip in enumerate(clip_input):
        result.append(transform_fn(clip))

    clip_input = np.stack(result, axis=0)
    clip_input = clip_input.reshape((-1,) + (36, 3, 224, 224))
    clip_input = np.transpose(clip_input, (0, 2, 1, 3, 4))
    
    return clip_input



def plot_one_box(x, img, color=[100,100,100], text_info="None",
                 velocity=None, thickness=1, fontsize=0.5, fontthickness=1):
    # Plots one bounding box on image img
    c1, c2 = (int(x[0]), int(x[1])), (int(x[2]), int(x[3]))
    c3=int((int(x[0])+int(x[2]))/2),int((int(x[1])+int(x[3]))/2)
    c4, c5 = (max(0,int(int(x[0])-(int(x[2])-int(x[0]))/2)),max(0,int(int(x[1])-(int(x[3])-int(x[1]))/2))),(int(int(x[2])+(int(x[2])-int(x[0]))/2),int(int(x[3])+(int(x[3])-int(x[1]))/2))
    cv2.line(img,c3,c3,color,5)
    cv2.rectangle(img, c1, c2, color, thickness, lineType=cv2.LINE_AA)
    t_size = cv2.getTextSize(text_info, cv2.FONT_HERSHEY_TRIPLEX, fontsize , fontthickness+2)[0]
    cv2.rectangle(img, c1, (c1[0] + int(t_size[0]), c1[1] + int(t_size[1]*1.45)), color, -1)
    cv2.rectangle(img, c4, c5, color, thickness, lineType=cv2.LINE_AA)
    cv2.putText(img, text_info, (c1[0], c1[1]+t_size[1]+2), 
                cv2.FONT_HERSHEY_TRIPLEX, fontsize, [255,255,255], fontthickness)
    return img

def deepsort_update(Tracker, pred, xywh, np_img):
    outputs = Tracker.update(xywh, pred[:,4:5],pred[:,5].tolist(),cv2.cvtColor(np_img,cv2.COLOR_BGR2RGB))
    return outputs





def save_yolopreds_tovideo(yolo_preds, id_to_swim_labels, color_map, output_video, vis=False):
    for i, (im, pred) in enumerate(zip(yolo_preds.ims, yolo_preds.pred)):
        im=cv2.cvtColor(im,cv2.COLOR_BGR2RGB)
        if pred.shape[0]:
            for j, (*box, cls, trackid, vx, vy) in enumerate(pred):
                if int(cls) != 0:
                    swim_label = ''
                elif trackid in id_to_swim_labels.keys():
                    swim_label = id_to_swim_labels[trackid].split(' ')[0]
                else:
                    swim_label = 'Unknow'
                text = '{} {} {}'.format(int(trackid),yolo_preds.names[int(cls)],swim_label)
                color = color_map[int(cls)]
                if yolo_preds.names[int(cls)]=='person':
                    im = plot_one_box(box,im,color,text)
                    
        im = im.astype(np.uint8)
        im = cv2.cvtColor(im, cv2.COLOR_BGR2RGB)
        output_video.write(im)
        if vis:
            im=cv2.cvtColor(im,cv2.COLOR_RGB2BGR)
            cv2.imshow("demo", im)

def main(config):
    device = config.device
    imsize = config.imsize
    drown_list = deque()
    
    model = torch.hub.load('ultralytics/yolov5', 'yolov5l6').to(device)
    model.conf = config.conf
    model.iou = config.iou
    model.max_det = 100
    if config.classes:
        model.classes = config.classes
    
    video_model = get_model(name='slowfast_4x16_resnet50_kinetics400', nclass=3)
    video_model.load_parameters("net.params")
    
    deepsort_tracker = DeepSort("BackEnd/model/deep_sort/deep_sort/deep/checkpoint/ckpt.t7")    
    swim_labelnames,_ = AvaLabeledVideoFramePaths.read_label_map("BackEnd/model/selfutils/swim.pbtxt")
    coco_color_map = [[random.randint(0, 255) for _ in range(3)] for _ in range(80)]

    vide_save_path = config.output
    video=cv2.VideoCapture(config.input)
    width,height = int(video.get(3)),int(video.get(4))
    video.release()
    outputvideo = cv2.VideoWriter(vide_save_path,cv2.VideoWriter_fourcc(*'mp4v'), 25, (width,height))
    print("processing...")
    
    cap = MyVideoCapture(config.input)
    vr = decord.VideoReader(config.input)
    id_to_swim_labels = {}
    a=time.time()
    while not cap.end:
        ret, img = cap.read()
        if not ret:
            continue
        yolo_preds=model([img], size=imsize)
        yolo_preds.files=["img.jpg"]
        
        deepsort_outputs=[]
        for j in range(len(yolo_preds.pred)):
            temp=deepsort_update(deepsort_tracker,yolo_preds.pred[j].cpu(),yolo_preds.xywh[j][:,0:4].cpu(),yolo_preds.ims[j])
            if len(temp)==0:
                temp=np.ones((0,8))
            deepsort_outputs.append(temp.astype(np.float32))
            
        yolo_preds.pred=deepsort_outputs
        if len(cap.stack) == 64:
            print(f"processing {cap.idx // 64}th second clips")
            clip = cap.get_video_clip()
            if yolo_preds.pred[0].shape[0]:

                inputs = swim_inference_transform(clip, yolo_preds.pred[0][:,0:4], crop_size=imsize) # (tid, rgb, frames, width, height)
                # for i in range(36):
                #     img = Image.fromarray(np.transpose(np.transpose(inputs, (0, 2, 1, 3, 4))[0][i],(1,2,0)), 'RGB')
                #     img.save('test_imgs/img%s.jpg'%i)
                #     print("finish img saving")
                with torch.no_grad():
                    slowfaster_preds = []
                    for input in inputs:
                        input = [input]
                        pred = video_model(nd.array(input))
                        slowfaster_preds.append(pred)
                for tid,location, pred in zip(yolo_preds.pred[0][:,5], yolo_preds.pred[0][:,0:4], slowfaster_preds):
                    now_label = np.argmax(pred).asscalar()+1
                    id_to_swim_labels[tid] = swim_labelnames[now_label] # 객체 id와 행동라벨 매핑
                    d_code=False
                    print(id_to_swim_labels)
                    if swim_labelnames[now_label]=='drown':
                        if drown_list:
                            center_x=int((int(location[0])+int(location[2]))/2)
                            center_y=int((int(location[1])+int(location[3]))/2)
                            if (datetime.datetime.now()-drown_list[0][0]).seconds>60: #시간 지나면 삭제
                                drown_list.popleft()
                            for index,i in enumerate(drown_list):    
                                if center_x>=i[1][0] and center_x<=i[1][2] and center_y >=i[1][1] and center_y <=i[1][3]:
                                    drown_list[index][2]+=1
                                    if drown_list[index][2]==1:
                                        print('익사 1단계')
                                    elif drown_list[index][2]==2:
                                        print('익사 2단계')
                                    elif drown_list[index][2]>=3:
                                        print('익사 3단계')
                                    else:
                                        pass
                                    d_code=True
                            if d_code==False:
                                print('등록')
                                find_x1, find_y1 = max(0,int(int(location[0])-(int(location[2])-int(location[0]))/2)), max(0,int(int(location[1])-(int(location[3])-int(location[1]))/2))
                                find_x2, find_y2 = int(int(location[2])+(int(location[2])-int(location[0]))/2),int(int(location[3])+(int(location[3])-int(location[1]))/2)
                                drown_list.append([datetime.datetime.now(),[find_x1,find_y1,find_x2, find_y2],0])
                        else:
                            print('등록')
                            find_x1, find_y1 = max(0,int(int(location[0])-(int(location[2])-int(location[0]))/2)), max(0,int(int(location[1])-(int(location[3])-int(location[1]))/2))
                            find_x2, find_y2 = int(int(location[2])+(int(location[2])-int(location[0]))/2),int(int(location[3])+(int(location[3])-int(location[1]))/2)
                            drown_list.append([datetime.datetime.now(),[find_x1,find_y1,find_x2, find_y2],0])

        save_yolopreds_tovideo(yolo_preds, id_to_swim_labels, coco_color_map, outputvideo, config.show)
    print(drown_list)
    print("total cost: {:.3f} s, video length: {} s".format(time.time()-a, cap.idx / 25))
    
    cap.release()
    outputvideo.release()
    print('saved video to:', vide_save_path)
    
    
if __name__=="__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', type=str, default="./home/wufan/images/video/ahpuh.mp4", help='test imgs folder or video or camera')
    parser.add_argument('--output', type=str, default="output.mp4", help='folder to save result imgs, can not use input folder')
    # object detect config
    parser.add_argument('--imsize', type=int, default=1080, help='inference size (pixels)')
    parser.add_argument('--conf', type=float, default=0.4, help='object confidence threshold')
    parser.add_argument('--iou', type=float, default=0.4, help='IOU threshold for NMS')
    parser.add_argument('--device', default='cuda', help='cuda device, i.e. 0 or 0,1,2,3 or cpu')
    parser.add_argument('--classes', nargs='+', type=int, help='filter by class: --class 0, or --class 0 2 3')
    parser.add_argument('--show', action='store_true', help='show img')
    config = parser.parse_args()
    
    if config.input.isdigit():
        print("using local camera.")
        config.input = int(config.input)
        
    print(config)
    main(config)
