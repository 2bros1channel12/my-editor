"""Validated source-time framing, compiled only to application-owned filters."""
from copy import deepcopy
import math


def validate_reframe(value, duration):
    if not isinstance(value,dict) or set(value)-{'enabled','locked','mode','subjectId','keyframes','subjects','keepBoth','layout','regions','detector'}:
        raise ValueError('Invalid reframing settings.')
    r=deepcopy(value)
    if any(type(r.get(k)) is not bool for k in ('enabled','locked')) or r.get('mode') not in ('crop','fit'):
        raise ValueError('Framing requires enabled/locked booleans and crop/fit mode.')
    if r.get('subjectId') is not None and (not isinstance(r['subjectId'],str) or len(r['subjectId'])>120): raise ValueError('Invalid subject ID.')
    frames=r.get('keyframes')
    if not isinstance(frames,list) or len(frames)>256: raise ValueError('Use at most 256 framing keyframes per clip.')
    previous=-1
    for f in frames:
        if not isinstance(f,dict) or not {'sourceTicks','x','y','width','height'}<=set(f) or set(f)-{'sourceTicks','x','y','width','height','layoutMode','confidence','subjectId','locked','explanation'}: raise ValueError('Invalid crop keyframe.')
        if type(f['sourceTicks']) is not int or not previous<f['sourceTicks']<=duration: raise ValueError('Crop timestamps must increase within the source bounds.')
        previous=f['sourceTicks']
        for k in ('x','y','width','height'):
            if isinstance(f[k],bool) or not isinstance(f[k],(float,int)) or not math.isfinite(f[k]) or not 0<=f[k]<=1: raise ValueError('Invalid normalized crop.')
        if min(f['width'],f['height'])<.05 or f['x']+f['width']>1.000001 or f['y']+f['height']>1.000001: raise ValueError('Crop leaves the original frame.')
        if f.get('layoutMode','crop') not in ('crop','fit'): raise ValueError('Invalid crop mode.')
        if 'confidence' in f and (isinstance(f['confidence'],bool) or not isinstance(f['confidence'],(int,float)) or not math.isfinite(f['confidence']) or not 0<=f['confidence']<=1):
            raise ValueError('Invalid crop confidence.')
        if 'locked' in f and type(f['locked']) is not bool: raise ValueError('Invalid crop lock.')
        if f.get('subjectId') is not None and (not isinstance(f['subjectId'],str) or len(f['subjectId'])>120): raise ValueError('Invalid keyframe subject.')
        if 'explanation' in f and (not isinstance(f['explanation'],str) or len(f['explanation'])>4000): raise ValueError('Invalid crop explanation.')
    if 'keepBoth' in r and type(r['keepBoth']) is not bool: raise ValueError('Invalid group framing choice.')
    if r.get('layout','single') not in ('single','stacked'): raise ValueError('Unsupported composition layout.')
    if r.get('layout')=='stacked':
        regions=r.get('regions')
        if not isinstance(regions,list) or len(regions)!=2: raise ValueError('Stacked layout requires reviewed top and bottom source regions.')
        for region in regions:
            if not isinstance(region, dict) or set(region) != {'x','y','width','height'}:
                raise ValueError('Composition regions contain only normalized numeric bounds.')
            validate_reframe(dict(enabled=True,locked=False,mode='crop',keyframes=[{'sourceTicks':0,**region}]),duration)
    return r


def aspect_rect(frame,sw,sh,ow,oh):
    ratio=(ow/oh)/(sw/sh)
    w,h=frame['width'],frame['height']; cx=frame['x']+w/2;cy=frame['y']+h/2
    if w/h<ratio:w=h*ratio
    else:h=w/ratio
    scale=min(1,1/w,1/h);w*=scale;h*=scale
    return dict(x=max(0,min(1-w,cx-w/2)),y=max(0,min(1-h,cy-h/2)),width=w,height=h)


def _expression(points,key,start):
    # All values have passed closed numeric validation; no strings from a model
    # or imported content can become filter syntax.
    result=f"{points[-1][key]:.9f}"
    for a,b in reversed(list(zip(points,points[1:]))):
        t0=(a['sourceTicks']-start)/1e6;t1=(b['sourceTicks']-start)/1e6
        lerp=f"({a[key]:.9f}+({b[key]-a[key]:.9f})*clip((it-{t0:.6f})/{t1-t0:.6f},0,1))"
        result=f"if(lt(it,{t1:.6f}),{lerp},{result})"
    return result


def framing_filter(value,sw,sh,ow,oh,source_in,source_duration,fps,label='rf'):
    r=validate_reframe(value,source_duration)
    if not r['enabled'] or r['mode']=='fit' or any(f.get('layoutMode')=='fit' for f in r['keyframes']):return ''
    if r.get('layout')=='stacked':
        chains=[]
        for i,region in enumerate(r['regions']):
            # Each reviewed panel is aspect fitted; never stretch a source region.
            w=max(2,int(sw*region['width'])//2*2);h=max(2,int(sh*region['height'])//2*2)
            chains.append(f"[{label}{i}]crop={w}:{h}:{int(sw*region['x'])}:{int(sh*region['y'])},scale={ow}:{oh//2}:force_original_aspect_ratio=decrease,pad={ow}:{oh//2}:(ow-iw)/2:(oh-ih)/2[{label}p{i}]")
        return f",split=2[{label}0][{label}1];"+';'.join(chains)+f';[{label}p0][{label}p1]vstack=inputs=2'
    points=[{'sourceTicks':f['sourceTicks'],**aspect_rect(f,sw,sh,ow,oh)} for f in r['keyframes']]
    if not points:
        points=[{'sourceTicks':source_in,**aspect_rect(dict(x=0,y=0,width=1,height=1),sw,sh,ow,oh)}]
        # Default center crop, rather than shrinking to fit the whole source.
        ratio=(ow/oh)/(sw/sh);w=min(1,ratio);h=min(1,1/ratio)
        points=[dict(sourceTicks=source_in,x=(1-w)/2,y=(1-h)/2,width=w,height=h)]
    canvas_w=max(sw,math.ceil(sh*ow/oh/2)*2);canvas_h=max(sh,math.ceil(sw*oh/ow/2)*2)
    for p in points:
        p['zoom']=canvas_w/(sw*p['width']);p['px']=p['x']*sw+(canvas_w-sw)/2;p['py']=p['y']*sh+(canvas_h-sh)/2
        if p['zoom']>10: raise ValueError('Crop exceeds the supported 10× zoom. Enlarge the crop.')
    zoom = f"{canvas_w/sw:.9f}/({_expression(points,'width',source_in)})"
    return f",pad={canvas_w}:{canvas_h}:(ow-iw)/2:(oh-ih)/2,fps={fps},zoompan=z='{zoom}':x='{_expression(points,'px',source_in)}':y='{_expression(points,'py',source_in)}':d=1:s={ow}x{oh}:fps={fps}"
