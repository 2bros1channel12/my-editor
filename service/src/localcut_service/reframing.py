"""Local spatial subject tracking; boxes and crops use upright normalized coordinates.

Track IDs describe continuity within one analysis only, never biometric identity.
Audio/visual speaker attribution is deliberately outside this detector's claims.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
from typing import Any, Protocol

VERSION = "spatial-reframe-3"


@dataclass(frozen=True)
class Detection:
    x: float
    y: float
    width: float
    height: float
    confidence: float
    kind: str = "face"

    def valid(self) -> bool:
        return (all(math.isfinite(v) for v in (self.x, self.y, self.width, self.height, self.confidence))
                and 0 <= self.x < 1 and 0 <= self.y < 1 and 0 < self.width <= 1
                and 0 < self.height <= 1 and self.x + self.width <= 1.000001
                and self.y + self.height <= 1.000001 and 0 <= self.confidence <= 1)


class SubjectDetector(Protocol):
    name: str
    version: str
    def detect(self, image: Any) -> list[Detection]: ...


def salient_region(image: Any) -> list[Detection]:
    """Conservative border-color contrast proposal, not object recognition.

    Requires one dominant bounded region against a reasonably uniform border.
    Busy scenes, blank frames and ambiguous equally prominent regions abstain.
    The returned confidence is a contrast signal, not semantic certainty.
    """
    import cv2
    import numpy as np
    height,width=image.shape[:2]
    if min(height,width)<24:return []
    small=cv2.resize(image,(min(160,width),min(160,height)),interpolation=cv2.INTER_AREA)
    h,w=small.shape[:2]
    lab=cv2.cvtColor(small,cv2.COLOR_BGR2LAB).astype(np.float32)
    border=np.concatenate((lab[0],lab[-1],lab[:,0],lab[:,-1]))
    background=np.median(border,axis=0)
    border_deviation=np.linalg.norm(border-background,axis=1)
    if float(np.percentile(border_deviation,85))>24:return []
    contrast=np.linalg.norm(lab-background,axis=2)
    mask=(contrast>max(40.,float(np.percentile(border_deviation,95))*2)).astype(np.uint8)
    mask=cv2.morphologyEx(mask,cv2.MORPH_OPEN,np.ones((3,3),np.uint8))
    count,labels,stats,_=cv2.connectedComponentsWithStats(mask,connectivity=8)
    candidates=[]
    for index in range(1,count):
        x,y,bw,bh,area=(int(v) for v in stats[index])
        if .015<=area/(w*h)<=.5 and x>0 and y>0 and x+bw<w and y+bh<h:
            candidates.append((area,index,x,y,bw,bh))
    if not candidates:return []
    area,index,x,y,bw,bh=max(candidates)
    if area/max(1,sum(c[0] for c in candidates))<.7:return []
    strength=float(np.mean(contrast[labels==index]))
    if strength<45:return []
    margin=.025
    left=max(0,x/w-margin);top=max(0,y/h-margin)
    right=min(1,(x+bw)/w+margin);bottom=min(1,(y+bh)/h+margin)
    return [Detection(left,top,right-left,bottom-top,min(.68,.55+(strength-40)/1000),"salient-region")]


class OpenCVSubjectDetector:
    """Bundled Haar frontal faces and HOG people; no network or model download."""
    name = "opencv-haar-hog-saliency"

    def __init__(self) -> None:
        try:
            import cv2
        except ImportError as exc:
            raise RuntimeError("Subject detection needs the optional local opencv-python-headless backend. Install it, or use center/manual reframing.") from exc
        self.cv2 = cv2
        self.version = f"{cv2.__version__}:saliency-1"
        self.faces = cv2.CascadeClassifier(str(Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"))
        if self.faces.empty():
            raise RuntimeError("The installed OpenCV face cascade is missing. Reinstall the local detector or use manual framing.")
        self.people = cv2.HOGDescriptor()
        self.people.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())

    def detect(self, image: Any) -> list[Detection]:
        cv2 = self.cv2
        height, width = image.shape[:2]
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        faces = self.faces.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(24, 24))
        result = [Detection(float(x/width), float(y/height), float(w/width), float(h/height), .8) for x,y,w,h in faces]
        # HOG is unreliable on tiny images; faces take priority to avoid double tracks.
        if not result and width >= 64 and height >= 128:
            boxes, weights = self.people.detectMultiScale(image, winStride=(8, 8), padding=(8, 8), scale=1.05)
            result = [Detection(float(x/width), float(y/height), float(min(w,width-x)/width), float(min(h,height-y)/height), min(.85, max(.3, float(score)/3)), "person") for (x,y,w,h),score in zip(boxes,weights)]
        result=[box for box in result if box.valid() and box.confidence>=.55]
        return result or salient_region(image)


def _center(box: Detection) -> tuple[float, float]:
    return box.x + box.width/2, box.y + box.height/2


def build_reframe_keyframes(samples: list[tuple[float, list[Detection]]], source_width: int,
        source_height: int, output_width: int, output_height: int, *, selected_subject_id: str | None = None,
        keep_both: bool = False, minimum_confidence: float = .55, maximum_speed: float = .18,
        smoothing_seconds: float = .7, ticks_per_second: int = 1_000_000, safe_area: dict[str,float] | None = None, check_cancelled=lambda: None) -> dict[str, Any]:
    """Produce editable source-time crops and temporary subject choices.

    Samples must use the source media elapsed clock, after rotation normalization.
    Empty/uncertain samples center-crop. Wide groups use fit rather than clipping.
    """
    if any(type(v) is not int or v <= 0 for v in (source_width, source_height, output_width, output_height)):
        raise ValueError("Source and output dimensions must be positive integers.")
    if type(ticks_per_second) is not int or ticks_per_second <= 0:
        raise ValueError("Source timebase must be a positive integer.")
    if not (0 <= minimum_confidence <= 1 and 0 < maximum_speed <= 1 and smoothing_seconds > 0):
        raise ValueError("Invalid tracking confidence or smoothing settings.")
    enforce_safe=safe_area is not None
    safe_area=safe_area or dict(left=0.,right=0.,top=0.,bottom=0.)
    if (set(safe_area)!={'left','right','top','bottom'} or any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) or not 0<=v<.5 for v in safe_area.values())
            or safe_area['left']+safe_area['right']>=1 or safe_area['top']+safe_area['bottom']>=1):raise ValueError('Invalid framing safe area.')
    aspect = output_width/output_height / (source_width/source_height)
    crop_w, crop_h = min(1., aspect), min(1., 1/aspect)
    tracks: dict[str, tuple[Detection, float]] = {}
    subjects: dict[str, dict[str, Any]] = {}
    frames = []
    previous_time = -1.
    previous_center = (.5, .5)
    primary = selected_subject_id
    counter = 0
    for timestamp, detections in samples:
        check_cancelled()
        if not isinstance(timestamp, (int,float)) or not math.isfinite(timestamp) or timestamp < 0 or timestamp <= previous_time:
            raise ValueError("Frame timestamps must be finite, nonnegative and strictly increasing.")
        current: dict[str, Detection] = {}
        for box in sorted(detections, key=lambda d: d.width*d.height, reverse=True):
            if not box.valid(): raise ValueError("Detection boxes must remain inside the source frame.")
            if box.confidence < minimum_confidence: continue
            candidates = [(math.dist(_center(box), _center(old)), key) for key,(old,last) in tracks.items()
                          if key not in current and timestamp-last <= 2 and old.kind == box.kind]
            distance,key = min(candidates, default=(10., ""))
            if distance > .3:
                counter += 1
                key = f"subject-{counter}"
            current[key] = box
            tracks[key] = (box,timestamp)
            subjects.setdefault(key, {"id": key, "kind": box.kind, "firstSourceTime": timestamp, "label": f"{box.kind.title()} {key.split('-')[-1]}"})
        if primary is None and current: primary = next(iter(current))
        chosen = list(current.values()) if keep_both else ([current[primary]] if primary in current else [])
        fit = False
        confidence = min((box.confidence for box in chosen), default=0.)
        if chosen:
            left = min(b.x for b in chosen); right = max(b.x+b.width for b in chosen)
            top = min(b.y for b in chosen); bottom = max(b.y+b.height for b in chosen)
            target = ((left+right)/2, (top+bottom)/2)
            fit = keep_both and (right-left > crop_w*.9 or bottom-top > crop_h*.9)
            reason = "Keep visible subjects together using fit." if fit else "Follow selected spatial subject; speaker identity is unverified."
            if any(box.kind=='salient-region' for box in chosen):
                reason="Follow prominent visual contrast region; signal strength is not object identity or speaker confidence."
            # Feasible crop origins place the entire important box in usable area.
            xmin=max(0.,right-(1-safe_area['right'])*crop_w)
            xmax=min(1-crop_w,left-safe_area['left']*crop_w)
            ymin=max(0.,bottom-(1-safe_area['bottom'])*crop_h)
            ymax=min(1-crop_h,top-safe_area['top']*crop_h)
            if enforce_safe and (xmin>xmax+1e-9 or ymin>ymax+1e-9):
                fit=True;reason='Subject cannot fit within the configured safe area; fit the source and review platform overlays.'
            elif enforce_safe and not fit:
                target=(max(xmin,min(xmax,target[0]-crop_w/2))+crop_w/2,max(ymin,min(ymax,target[1]-crop_h/2))+crop_h/2)
        else:
            target = (.5,.5)
            reason = "Low confidence or selected subject absent: center crop."
        target = (max(crop_w/2,min(1-crop_w/2,target[0])),max(crop_h/2,min(1-crop_h/2,target[1])))
        if previous_time >= 0:
            dt = timestamp-previous_time
            alpha = 1-math.exp(-dt/smoothing_seconds)
            if math.dist(target, previous_center) < .01:
                target = previous_center
            delta = tuple((target[i]-previous_center[i])*alpha for i in range(2))
            length = math.hypot(*delta)
            factor = min(1., maximum_speed*dt/max(length,1e-9))
            target = tuple(previous_center[i]+delta[i]*factor for i in range(2))
            if enforce_safe and chosen and not fit and (target[0]-crop_w/2<xmin-1e-8 or target[0]-crop_w/2>xmax+1e-8 or target[1]-crop_h/2<ymin-1e-8 or target[1]-crop_h/2>ymax+1e-8):
                fit=True;reason='Smooth crop cannot retain the subject inside the safe area; fit the source for review.'
        previous_center = target
        previous_time = timestamp
        frames.append({"sourceTicks":round(timestamp*ticks_per_second),"x":0. if fit else target[0]-crop_w/2,"y":0. if fit else target[1]-crop_h/2,
                       "width":1. if fit else crop_w,"height":1. if fit else crop_h,"layoutMode":"fit" if fit else "crop",
                       "confidence":confidence,"subjectId":primary,"locked":False,"explanation":reason})
    return {"version":VERSION,"subjects":list(subjects.values()),"keyframes":frames,"selectedSubjectId":primary}


def detect_video_samples(path: Path, timestamps: list[float], context: Any, *, ffmpeg: str = "ffmpeg",
                         detector: SubjectDetector | None = None) -> list[tuple[float,list[Detection]]]:
    """Bounded sparse FFmpeg sampling, autorotated, with cancellable process lifetime."""
    from .proxies import MediaProcess
    detector = detector or OpenCVSubjectDetector()
    try:
        import cv2
        import numpy as np
    except ImportError as exc:
        raise RuntimeError("Install the local OpenCV backend or use manual framing.") from exc
    if len(timestamps) > 3600: raise ValueError("Limit one subject analysis to 3600 sampled frames.")
    if any(not math.isfinite(t) or t < 0 for t in timestamps): raise ValueError("Invalid source sampling time.")
    results = []
    for index,timestamp in enumerate(timestamps):
        context.check_cancelled()
        data = MediaProcess().run([ffmpeg,"-hide_banner","-loglevel","error","-nostdin","-protocol_whitelist","file,pipe","-ss",str(timestamp),"-i",str(path),
            "-frames:v","1","-vf","scale=w='if(gte(dar,1),640,max(2,trunc(640*dar/2)*2))':h='if(gte(dar,1),max(2,trunc(640/dar/2)*2),640)',setsar=1","-f","image2pipe","-vcodec","png","pipe:1"],context,timeout=60,stage="sampling subjects")
        if not data:
            raise ValueError(f"No decodable video frame at source time {timestamp}. Trim the source interval or regenerate inspected media metadata.")
        frame = cv2.imdecode(np.frombuffer(data,dtype=np.uint8),cv2.IMREAD_COLOR)
        if frame is None: raise ValueError(f"Could not decode subject sample at source time {timestamp}.")
        boxes = detector.detect(frame)
        context.check_cancelled()
        results.append((timestamp,boxes))
        context.report((index+1)/max(1,len(timestamps)),"detecting local subjects")
    return results
