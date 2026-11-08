"""Conservative, local face detection for exported stills and GIF frames."""
from pathlib import Path
import threading

import cv2
import numpy as np
from PIL import Image


MODEL_PATH = Path(__file__).resolve().parent / 'assets' / 'face_detection_yunet_2023mar.onnx'
FACE_PROCESSING_VERSION = 'yunet-2023mar-strict-v1'
MIN_CONFIDENCE = 0.90
_detectors = threading.local()


def valid_face(row):
    """Require a confident face and plausible five-point facial geometry."""
    if len(row) != 15 or not np.isfinite(row).all():
        return False
    x, y, w, h = row[:4]
    if row[14] < MIN_CONFIDENCE or min(w, h) < 20 or not 0.5 <= w / h <= 1.6:
        return False
    points = row[4:14].reshape(5, 2)
    if not ((points[:, 0] >= x - w * .1) & (points[:, 0] <= x + w * 1.1)
            & (points[:, 1] >= y - h * .1) & (points[:, 1] <= y + h * 1.1)).all():
        return False
    eye_a, eye_b, nose, mouth_a, mouth_b = points
    eyes = (eye_a + eye_b) / 2
    mouth = (mouth_a + mouth_b) / 2
    axis = mouth - eyes
    distance = np.linalg.norm(axis)
    if not .15 * h <= distance <= .8 * h:
        return False
    if not .15 * w <= np.linalg.norm(eye_a - eye_b) <= .85 * w:
        return False
    if not .1 * w <= np.linalg.norm(mouth_a - mouth_b) <= .8 * w:
        return False
    # The nose should lie between the eye line and mouth, including tilted faces.
    projection = np.dot(nose - eyes, axis) / (distance * distance)
    lateral = np.linalg.norm(nose - eyes - projection * axis)
    return bool(.05 <= projection <= 1.05 and lateral <= w * .3)


def mosaic_faces(image):
    image = image.convert('RGB')
    if not MODEL_PATH.is_file():
        raise RuntimeError('얼굴 감지 모델이 없습니다. assets/face_detection_yunet_2023mar.onnx를 배포에 포함해 주세요.')
    detector = getattr(_detectors, 'model', None)
    if detector is None:
        detector = cv2.FaceDetectorYN.create(str(MODEL_PATH), '', (320, 320), MIN_CONFIDENCE, .3, 5000)
        _detectors.model = detector
    pixels = np.array(image)
    width, height = image.size
    if min(width, height) < 20:
        return image
    detector.setInputSize((width, height))
    _, faces = detector.detect(cv2.cvtColor(pixels, cv2.COLOR_RGB2BGR))
    # A second image scale must independently confirm the same region.
    check_width, check_height = max(20, round(width * .8)), max(20, round(height * .8))
    check = cv2.resize(pixels, (check_width, check_height))
    detector.setInputSize((check_width, check_height))
    _, repeated = detector.detect(cv2.cvtColor(check, cv2.COLOR_RGB2BGR))
    confirmed = []
    for other in repeated if repeated is not None else []:
        scaled = other.copy()
        scaled[[0, 2, 4, 6, 8, 10, 12]] *= width / check_width
        scaled[[1, 3, 5, 7, 9, 11, 13]] *= height / check_height
        if valid_face(scaled):
            confirmed.append(scaled)
    for row in faces if faces is not None else []:
        if not valid_face(row):
            continue
        x, y, w, h = row[:4]
        def overlaps(other):
            ox, oy, ow, oh = other[:4]
            intersection = max(0, min(x + w, ox + ow) - max(x, ox)) * max(0, min(y + h, oy + oh) - max(y, oy))
            return intersection / max(1, w * h + ow * oh - intersection) >= .5
        if not any(overlaps(other) for other in confirmed):
            continue
        left, top = max(0, int(x - w * .08)), max(0, int(y - h * .1))
        right, bottom = min(width, int(np.ceil(x + w * 1.08))), min(height, int(np.ceil(y + h * 1.1)))
        if right <= left or bottom <= top:
            continue
        face = Image.fromarray(pixels[top:bottom, left:right])
        small = (max(1, face.width // 12), max(1, face.height // 12))
        pixels[top:bottom, left:right] = np.asarray(
            face.resize(small, Image.Resampling.BILINEAR).resize(face.size, Image.Resampling.NEAREST))
    return Image.fromarray(pixels)
