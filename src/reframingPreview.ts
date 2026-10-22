/** Source coordinates use the displayed, rotation-corrected source frame. */
export interface CropKeyframe { sourceTicks: number; x: number; y: number; width: number; height: number; layoutMode?:'crop'|'fit';confidence?:number;subjectId?:string|null;locked?:boolean;explanation?:string }
export interface SafeArea { top: number; right: number; bottom: number; left: number }
const clamp = (n: number, min: number, max: number) => Math.max(min, Math.min(max, n));
export function validateCrop(key: CropKeyframe): boolean {
  return [key.sourceTicks,key.x,key.y,key.width,key.height].every(Number.isFinite) && Number.isSafeInteger(key.sourceTicks) && key.sourceTicks >= 0 && key.width > 0 && key.height > 0 && key.width <= 1 && key.height <= 1 && key.x >= 0 && key.y >= 0 && key.x + key.width <= 1.000001 && key.y + key.height <= 1.000001;
}
export function centerCrop(sourceAspect: number, outputAspect: number, sourceTicks = 0): CropKeyframe {
  if (!(sourceAspect > 0 && outputAspect > 0 && Number.isFinite(sourceAspect) && Number.isFinite(outputAspect))) throw new Error('Frame aspects must be positive.');
  const width = Math.min(1, outputAspect / sourceAspect), height = Math.min(1, sourceAspect / outputAspect);
  return { sourceTicks, x: (1 - width) / 2, y: (1 - height) / 2, width, height };
}
/** Match the renderer's piecewise linear movement; no browser-only easing. */
export function interpolateCrop(keys: readonly CropKeyframe[], sourceTicks: number, fallback: CropKeyframe): CropKeyframe {
  if (!keys.length) return { ...fallback, sourceTicks };
  const sorted = [...keys].sort((a, b) => a.sourceTicks - b.sourceTicks);
  if (sorted.some(k => !validateCrop(k))) throw new Error('Invalid crop keyframe.');
  const next = sorted.findIndex(k => k.sourceTicks > sourceTicks);
  if (next === 0) return { ...sorted[0], sourceTicks };
  if (next < 0) return { ...sorted[sorted.length - 1], sourceTicks };
  const a = sorted[next - 1], b = sorted[next], amount = (sourceTicks - a.sourceTicks) / (b.sourceTicks - a.sourceTicks);
  return { sourceTicks, x: a.x + (b.x - a.x) * amount, y: a.y + (b.y - a.y) * amount, width: a.width + (b.width - a.width) * amount, height: a.height + (b.height - a.height) * amount };
}
/** Resize always preserves output shape, so crop controls cannot stretch footage. */
export function resizeCrop(crop: CropKeyframe, width: number, sourceAspect: number, outputAspect: number): CropKeyframe {
  const w = clamp(width, Math.max(.05,.05*outputAspect/sourceAspect), Math.min(1, outputAspect / sourceAspect)), h = w * sourceAspect / outputAspect;
  return { ...crop, width: w, height: h, x: clamp(crop.x + (crop.width - w) / 2, 0, 1 - w), y: clamp(crop.y + (crop.height - h) / 2, 0, 1 - h) };
}
export function aspectCrop(crop:CropKeyframe,sourceAspect:number,outputAspect:number):CropKeyframe{
 const ratio=outputAspect/sourceAspect,cx=crop.x+crop.width/2,cy=crop.y+crop.height/2;let w=crop.width,h=crop.height;if(w/h<ratio)w=h*ratio;else h=w/ratio;const scale=Math.min(1,1/w,1/h);w*=scale;h*=scale;return {...crop,width:w,height:h,x:clamp(cx-w/2,0,1-w),y:clamp(cy-h/2,0,1-h)};
}
export function moveCrop(crop: CropKeyframe, x: number, y: number): CropKeyframe {
  return { ...crop, x: clamp(x, 0, 1 - crop.width), y: clamp(y, 0, 1 - crop.height) };
}
export function upsertCropKeyframe(keys: readonly CropKeyframe[], key: CropKeyframe): CropKeyframe[] {
  if (!validateCrop(key)) throw new Error('Invalid crop keyframe.');
  return [...keys.filter(k => k.sourceTicks !== key.sourceTicks), key].sort((a, b) => a.sourceTicks - b.sourceTicks);
}
