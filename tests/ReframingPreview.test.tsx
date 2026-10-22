import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import ReframingPreview, { type ReframeSettings } from '../src/ui/ReframingPreview';
import { centerCrop } from '../src/reframingPreview';

const reframe: ReframeSettings = { enabled: true, locked: false, mode: 'crop', subjectId: null, keyframes: [centerCrop(16 / 9, 9 / 16)] };
const props = { playbackUrl: 'local-preview.mp4', sourceTicks: 1000000, sourceWidth: 1920, sourceHeight: 1080, outputAspect: 9 / 16, reframe, onChange: vi.fn() };
describe('reframing inspection', () => {
  it('shows source, output crop and configurable safe-area overlays', () => {
    render(<ReframingPreview {...props} safeArea={{ top: .1, right: .15, bottom: .25, left: .08 }} />);
    expect(screen.getByLabelText('Original framing preview')).toBeTruthy();
    expect(screen.getByLabelText('Output crop overlay')).toBeTruthy();
    expect(screen.getByLabelText('Caption and visual safe area').style.bottom).toBe('25%');
    expect(screen.getByLabelText('Caption and visual safe area').style.right).toBe('15%');
  });
  it('saves reviewed keyframes and selects a subject without modifying the original', () => {
    const save = vi.fn();
    render(<ReframingPreview {...props} onChange={save} subjects={[{ id: 'subject-2', label: 'Speaker 2' }]} />);
    fireEvent.change(screen.getByLabelText('Crop horizontal position'), { target: { value: '.2' } });
    fireEvent.click(screen.getByText('Save crop keyframe'));
    expect(save.mock.calls[0][0].keyframes.at(-1)).toMatchObject({ sourceTicks: 1000000, x: .2 });
    fireEvent.change(screen.getByLabelText('Follow subject'), { target: { value: 'subject-2' } });
    expect(save.mock.calls[1][0].subjectId).toBe('subject-2');
    expect(reframe.keyframes).toHaveLength(1);
  });
  it('locks automatic tracking and crop changes, while allowing explicit unlock', () => {
    render(<ReframingPreview {...props} reframe={{ ...reframe, locked: true }} />);
    expect(screen.getByLabelText('Follow subject')).toBeDisabled();
    expect(screen.getByText('Save crop keyframe')).toBeDisabled();
    expect(screen.getByLabelText('Lock crop')).not.toBeDisabled();
  });
  it('uses padding for keep-both-speakers mode or disabled crop', () => {
    const { rerender } = render(<ReframingPreview {...props} reframe={{ ...reframe, mode: 'fit', subjectId: 'all' }} />);
    expect(screen.queryByLabelText('Output crop overlay')).toBeNull();
    expect(screen.getByLabelText('Reframed output preview').style.objectFit).toBe('contain');
    rerender(<ReframingPreview {...props} reframe={{ ...reframe, enabled: false }} />);
    expect(screen.getByLabelText('Reframed output preview').style.objectFit).toBe('contain');
  });
  it('shows independently cropped same-source stacked regions and saves region geometry',()=>{
    const save=vi.fn();render(<ReframingPreview {...props} onChange={save} reframe={{...reframe,layout:'stacked',regions:[{x:0,y:0,width:1,height:.6},{x:.6,y:.6,width:.4,height:.4}]}}/>);
    expect(screen.getByLabelText('Stacked output preview')).toBeTruthy();
    expect(screen.getByLabelText('Top gameplay region')).toBeTruthy();
    expect(screen.getByLabelText('Bottom face camera region')).toBeTruthy();
    fireEvent.change(screen.getByLabelText('Bottom region x'),{target:{value:'.5'}});
    fireEvent.click(screen.getByText('Save stacked regions'));
    expect(save.mock.calls[0][0].regions[1].x).toBe(.5);
  });
});
