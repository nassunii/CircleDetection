import cv2
import numpy as np
import time
import psutil
import os
from gtts import gTTS
import torch
import torch.nn as nn
from torch.cuda.amp import autocast
import torch.nn.functional as F
from torch.utils.data import Dataset, DataLoader

class CircleDataset(Dataset):
    def __init__(self, img_dir, target_dir, enable_fp16=True):
        self.img_dir = img_dir
        self.target_dir = target_dir
        self.img_files = sorted(os.listdir(img_dir))
        self.target_files = sorted(os.listdir(target_dir))
        self.enable_fp16 = enable_fp16

    def __len__(self):
        return len(self.img_files)

    def __getitem__(self, idx):
        img_path = os.path.join(self.img_dir, self.img_files[idx])
        target_path = os.path.join(self.target_dir, self.target_files[idx])
        
        img = torch.load(img_path).float() / 255.0
        target = torch.load(target_path).size(0)
        
        if self.enable_fp16:
            img = img.half()
            target = torch.tensor([target], dtype=torch.float16)
        else:
            target = torch.tensor([target], dtype=torch.float32)
        
        return img.unsqueeze(0), target
    
    # 학습 데이터 로드
train_dataset = CircleDataset('train/img', 'train/target')
train_loader = DataLoader(train_dataset, batch_size=4, shuffle=True)

class CircleNet_Q(nn.Module):
    def __init__(self, enable_fp16=True):
        super().__init__()
        self.enable_fp16 = enable_fp16 and torch.cuda.is_available()
        
        self.features = nn.Sequential(
            nn.Conv2d(1, 16, kernel_size=3, padding=1),
            nn.ReLU(),
            nn.MaxPool2d(2),
            nn.Conv2d(16, 32, kernel_size=3, padding=1),
            nn.ReLU(),
            nn.MaxPool2d(2),
            nn.Conv2d(32, 64, kernel_size=3, padding=1),
            nn.ReLU(),
            nn.AdaptiveAvgPool2d((7, 7)) 
        )

        self.classifier = nn.Sequential(
            nn.Flatten(),
            nn.Linear(64 * 7 * 7, 128),
            nn.ReLU(),
            nn.Dropout(0.5),
            nn.Linear(128, 1)
        )
        
        self.apply(self._init_weights)
        
        # Convert model to FP16 if enabled
        if self.enable_fp16:
            self.half()
            
        if torch.cuda.is_available():
            self.cuda()
            torch.backends.cudnn.benchmark = True
            
    def _init_weights(self, m):
        if isinstance(m, (nn.Conv2d, nn.Linear)):
            nn.init.kaiming_normal_(m.weight, mode='fan_out', nonlinearity='relu')
            if m.bias is not None:
                nn.init.zeros_(m.bias)
            if self.enable_fp16:
                m.weight.data = m.weight.data.half()
                if m.bias is not None:
                    m.bias.data = m.bias.data.half()
    
    def forward(self, x):
        if self.enable_fp16 and x.dtype != torch.float16:
            x = x.half()
        
        with autocast(enabled=self.enable_fp16):
            x = self.features(x)
            x = self.classifier(x)
        
        return x

class CircleTrainer:
    def __init__(self, model, criterion, optimizer, device, enable_fp16=True):
        self.model = model
        self.criterion = criterion
        self.optimizer = optimizer
        self.device = device
        self.enable_fp16 = enable_fp16 and torch.cuda.is_available()  # GPU 있을 때만 FP16 사용
        self.scaler = torch.cuda.amp.GradScaler(enabled=self.enable_fp16)
    
    def train_step(self, inputs, targets):
        self.model.train()
        self.optimizer.zero_grad()
        
        inputs = inputs.to(self.device)
        targets = targets.to(self.device)
        
        if self.enable_fp16:
            inputs = inputs.half()
            targets = targets.half()
        
        with autocast(enabled=self.enable_fp16):
            outputs = self.model(inputs)
            loss = self.criterion(outputs, targets)
        
        if self.enable_fp16:
            self.scaler.scale(loss).backward()
            self.scaler.step(self.optimizer)
            self.scaler.update()
        else:
            loss.backward()
            self.optimizer.step()
        
        return loss.item()
    
def get_memory_usage():
    return psutil.Process(os.getpid()).memory_info().rss / 1024 / 1024

def detect_circles(frame, model, device, enable_fp16=True):
    enable_fp16 = enable_fp16 and torch.cuda.is_available()
    # 이미지 전처리
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    img = cv2.resize(gray, (416, 416))
    img_tensor = torch.FloatTensor(img).unsqueeze(0).unsqueeze(0).to(device) / 255.0
    
    # OpenCV로 원의 위치 검출
    blurred = cv2.GaussianBlur(gray, (9, 9), 2)
    thresh = cv2.adaptiveThreshold(
        blurred, 255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY_INV,
        11, 2
    )
    
    contours, _ = cv2.findContours(thresh, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    
    # 원 후보 검출
    circles = []
    for contour in contours:
        area = cv2.contourArea(contour)
        perimeter = cv2.arcLength(contour, True)
        
        if perimeter > 0:
            circularity = 4 * np.pi * area / (perimeter * perimeter)
            if circularity > 0.7 and area > 100:
                (x, y), radius = cv2.minEnclosingCircle(contour)
                circles.append((int(x), int(y), int(radius)))
    
    # 근접한 원들 병합
    merged_circles = []
    used = set()
    
    for i, (x1, y1, r1) in enumerate(circles):
        if i in used:
            continue
            
        current = [x1, y1, r1]
        count = 1
        
        for j, (x2, y2, r2) in enumerate(circles[i+1:], i+1):
            if j in used:
                continue
                
            # 두 원의 중심점 사이 거리 계산
            distance = np.sqrt((x1-x2)**2 + (y1-y2)**2)
            if distance < max(r1, r2):  # 원이 겹치는 경우
                current[0] += x2
                current[1] += y2
                current[2] += r2
                count += 1
                used.add(j)
                
        if count > 1:
            current = [int(c/count) for c in current]
            
        if i not in used:
            merged_circles.append(tuple(current))
    
    # 딥러닝 모델로 검증 (필요한 경우)
    with torch.no_grad():
        pred = model(img_tensor)
    
    return merged_circles, len(merged_circles) 

def measure_performance(frame, model, device, circles=None):
    start_time = time.time()
    circles, num_circles = detect_circles(frame, model, device)
    inference_time = (time.time() - start_time) * 1000

    return {
        'inference_time_ms': inference_time,
        'memory_mb': get_memory_usage(),
        'num_circles': num_circles,
        'circles': circles
    }

def gstreamer_pipeline(
    sensor_id=0,
    capture_width=640,
    capture_height=480,
    display_width=640,
    display_height=480,
    framerate=30,
    flip_method=0,
):
    return (
        "nvarguscamerasrc sensor-id=%d ! "
        "video/x-raw(memory:NVMM), width=(int)%d, height=(int)%d, framerate=(fraction)%d/1 ! "
        "nvvidconv flip-method=%d ! "
        "video/x-raw, width=(int)%d, height=(int)%d, format=(string)BGRx ! "
        "videoconvert ! "
        "video/x-raw, format=(string)BGR ! appsink"
        % (
            sensor_id,
            capture_width,
            capture_height,
            framerate,
            flip_method,
            display_width,
            display_height,
        )
    )

def main():
    # CPU 사용 (quantization은 CPU에서만 지원)
    enable_fp16 = False
    device = torch.device("cpu")
    print(f"Using device: {device}")

    train_dataset = CircleDataset('train/img', 'train/target', enable_fp16=enable_fp16)
    train_loader = DataLoader(train_dataset, batch_size=4, shuffle=True)
    
    model = CircleNet_Q(enable_fp16=True).to(device)
    criterion = nn.MSELoss()
    optimizer = torch.optim.Adam(model.parameters(), lr=0.001)
    trainer = CircleTrainer(model, criterion, optimizer, device, enable_fp16=enable_fp16)

    # 학습
    num_epochs = 2
    for epoch in range(num_epochs):
        running_loss = 0.0
        for imgs, targets in train_loader:
            loss = trainer.train_step(imgs, targets)
            running_loss += loss
            
        print(f"Epoch {epoch+1}/{num_epochs}, Loss: {running_loss/len(train_loader):.4f}")

    # 모델 저장
    torch.save(model.state_dict(), 'Q_model.pth')
    print("Trained model saved.")

    model.eval()
    print("Training completed. Starting camera...")
    
    # 카메라 실행
    cap = cv2.VideoCapture(gstreamer_pipeline(flip_method=0), cv2.CAP_GSTREAMER)
    
    while True:
        ret, frame = cap.read()
        if not ret:
            break
        
        metrics = measure_performance(frame, model, device)
        
        # 검출된 원 표시
        for (x, y, r) in metrics['circles']:
            cv2.circle(frame, (x, y), r, (0, 255, 0), 2)
            cv2.circle(frame, (x, y), 2, (0, 0, 255), 3)
        
        # 정보 표시
        cv2.putText(frame, f"Circles: {metrics['num_circles']}", (10, 30), 
                    cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 0), 2)
        cv2.putText(frame, f"Time: {metrics['inference_time_ms']:.1f}ms", (10, 60), 
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
        cv2.putText(frame, f"Memory: {metrics['memory_mb']:.1f}MB", (10, 90),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
        cv2.putText(frame, "Press 'c' to count circles", (10, 120),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
        
        cv2.imshow("Circles", frame)
        
        key = cv2.waitKey(1) & 0xFF
        if key == ord('q'):
            break
        elif key == ord('c'):
            text = f"{metrics['num_circles']}개의 원이 있습니다."
            tts = gTTS(text=text, lang='ko')
            tts.save("circles.wav")
            os.system("aplay circles.wav")  # Linux 명령어로 변경

    cap.release()
    cv2.destroyAllWindows()

if __name__ == "__main__":
    main()
