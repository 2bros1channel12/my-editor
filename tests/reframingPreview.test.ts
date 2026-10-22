import { describe, expect, it } from 'vitest';
import { centerCrop, interpolateCrop, moveCrop, resizeCrop, upsertCropKeyframe, validateCrop } from '../src/reframingPreview';

describe('source-normalized reframing geometry', () => {
  it('crops horizontal footage vertically without stretching', () => {
    const crop = centerCrop(16 / 9, 9 / 16);
    expect(crop.height).toBe(1);
    expect(crop.width * 16 / 9 / crop.height).toBeCloseTo(9 / 16);
    expect(validateCrop(crop)).toBe(true);
  });
  it('crops a vertical source horizontally, with shape retained while resizing', () => {
    const crop = resizeCrop(centerCrop(9 / 16, 16 / 9), .7, 9 / 16, 16 / 9);
    expect(crop.width * 9 / 16 / crop.height).toBeCloseTo(16 / 9);
    expect(validateCrop(crop)).toBe(true);
  });
  it('interpolates a moving subject continuously in source time and holds endpoints', () => {
    const a = { sourceTicks: 0, x: 0, y: 0, width: .3, height: 1 };
    const b = { ...a, sourceTicks: 1000000, x: .7 };
    expect(interpolateCrop([a, b], 500000, a).x).toBeCloseTo(.35);
    expect(interpolateCrop([a, b], 1500000, a).x).toBe(.7);
    expect(interpolateCrop([b], 0, a).x).toBe(.7);
  });
  it('clamps manual positions inside the original source', () => {
    const crop = moveCrop(centerCrop(16 / 9, 9 / 16), 2, -2);
    expect(crop.x + crop.width).toBeCloseTo(1);
    expect(crop.y).toBe(0);
    expect(validateCrop(crop)).toBe(true);
  });
  it('replaces only a keyframe at the exact source timestamp', () => {
    const a = centerCrop(16 / 9, 1, 0), b = { ...a, sourceTicks: 1000 };
    expect(upsertCropKeyframe([a, b], { ...a, x: .1 })).toEqual([{ ...a, x: .1 }, b]);
    expect(() => upsertCropKeyframe([a], { ...a, sourceTicks: -1 })).toThrow();
    expect(() => interpolateCrop([{ ...a, width: Number.NaN }], 0, a)).toThrow();
  });
  it('keeps independent horizontal and vertical variants', () => {
    const horizontal = [centerCrop(16 / 9, 16 / 9)], vertical = [centerCrop(16 / 9, 9 / 16)];
    const adjusted = upsertCropKeyframe(vertical, { ...vertical[0], x: 0 });
    expect(horizontal[0].width).toBe(1);
    expect(vertical[0].x).toBeGreaterThan(0);
    expect(adjusted[0].x).toBe(0);
  });
});
