"""Editable delivery constraints. Platform names are recipes, never engine rules."""
from copy import deepcopy
from fractions import Fraction
import math
import re

VERSION = 1

def profile_presets():
    rows = [('youtube','YouTube horizontal','horizontal',1920,1080,480,3600),('shorts','YouTube Shorts','vertical',1080,1920,45,180),('tiktok','TikTok','vertical',1080,1920,45,180),('reels','Instagram Reels','vertical',1080,1920,45,180),('square','Square','square',1080,1080,60,3600),('original','Original source','original',1920,1080,60,3600),('custom','Custom','custom',1920,1080,60,3600)]
    return [dict(schemaVersion=VERSION,id=i,name=n,platform=i,aspect=a,width=w,height=h,frameRate=30,targetDurationSeconds=t,maxDurationSeconds=m,safeArea={'top':.08,'right':.15 if a=='vertical' else .05,'bottom':.20 if a=='vertical' else .08,'left':.05},fit='crop') for i,n,a,w,h,t,m in rows]

def _number(v,name,lo,hi):
    if isinstance(v,bool) or not isinstance(v,(float,int)) or not math.isfinite(v) or not lo<=v<=hi: raise ValueError(f'{name} must be between {lo} and {hi}.')
    return v

def validate_profile(value):
    required={'schemaVersion','id','name','platform','aspect','width','height','frameRate','targetDurationSeconds','maxDurationSeconds','safeArea','fit'}
    if not isinstance(value,dict) or set(value)!=required or type(value['schemaVersion']) is not int or value['schemaVersion']!=VERSION: raise ValueError('Invalid output profile schema.')
    p=deepcopy(value)
    for k in ('id','name','platform'):
        if not isinstance(p[k],str) or not p[k].strip() or len(p[k])>120: raise ValueError(f'Invalid profile {k}.')
    if not isinstance(p['aspect'],str) or not isinstance(p['fit'],str) or p['aspect'] not in ('horizontal','vertical','square','original','custom') or p['fit'] not in ('crop','contain'): raise ValueError('Invalid output aspect or fit mode.')
    for k in ('width','height'):
        if type(p[k]) is not int or not 64<=p[k]<=7680 or p[k]%2: raise ValueError('Output dimensions must be even integers between 64 and 7680.')
    if p['width']*p['height']>33_177_600: raise ValueError('Output exceeds the supported pixel budget.')
    expected={'horizontal':16/9,'vertical':9/16,'square':1}.get(p['aspect'])
    if expected and abs(p['width']/p['height']-expected)>.005: raise ValueError('Resolution does not match the selected aspect ratio.')
    _number(p['frameRate'],'Frame rate',1,120)
    _number(p['targetDurationSeconds'],'Target duration',.1,86400)
    _number(p['maxDurationSeconds'],'Maximum duration',.1,86400)
    if p['targetDurationSeconds']>p['maxDurationSeconds']: raise ValueError('Target duration exceeds maximum duration.')
    s=p['safeArea']
    if not isinstance(s,dict) or set(s)!={'top','right','bottom','left'}: raise ValueError('Invalid safe area.')
    for k,v in s.items(): _number(v,f'Safe area {k}',0,.45)
    if s['left']+s['right']>=.8 or s['top']+s['bottom']>=.8: raise ValueError('Safe area leaves too little visible space.')
    return p

def resolve_profile(profile,source_width,source_height):
    p=validate_profile(profile)
    if type(source_width) is not int or type(source_height) is not int or min(source_width,source_height)<=0: raise ValueError('Source display dimensions are required.')
    if p['aspect']=='original':
        ratio=Fraction(source_width,source_height)
        unit_w,unit_h=ratio.numerator,ratio.denominator
        step=2 if unit_w%2 or unit_h%2 else 1
        multiplier=int(min(p['width']/unit_w,p['height']/unit_h,source_width/unit_w))//step*step
        if multiplier and min(unit_w*multiplier,unit_h*multiplier)>=64:
            p['width']=unit_w*multiplier; p['height']=unit_h*multiplier
        else:
            # Unusual coprime dimensions need padding rather than a distorted scale.
            p['width']=max(64,min(7680,(source_width+1)//2*2)); p['height']=max(64,min(7680,(source_height+1)//2*2)); p['fit']='contain'
    ratio=Fraction(p['width'],p['height'])
    return {**p,'aspectRatio':f'{ratio.numerator}:{ratio.denominator}'}

def caption_safe_rect(profile):
    p=validate_profile(profile); s=p['safeArea']
    return {'x':round(p['width']*s['left']),'y':round(p['height']*s['top']),'width':round(p['width']*(1-s['left']-s['right'])),'height':round(p['height']*(1-s['top']-s['bottom']))}

def parse_output_request(text):
    """Conservative local parser; returns suggestions requiring explicit review."""
    if not isinstance(text,str) or len(text)>16000: raise ValueError('Invalid output request.')
    t=text.casefold(); recipes=profile_presets(); key='youtube'
    if 'tiktok' in t: key='tiktok'
    elif 'reels' in t: key='reels'
    elif 'shorts' in t: key='shorts'
    elif 'square' in t: key='square'
    p=deepcopy(next(x for x in recipes if x['id']==key))
    if 'vertical' in t: p.update(aspect='vertical',width=1080,height=1920)
    elif 'horizontal' in t: p.update(aspect='horizontal',width=1920,height=1080)
    words={'one':1,'two':2,'three':3,'four':4,'five':5,'eight':8,'ten':10}
    match=re.search(r'\b(\d+(?:\.\d+)?|one|two|three|four|five|eight|ten)[ -]*(second|minute)s?\b',t)
    if match:
        v=words[match[1]] if match[1] in words else float(match[1]); duration=v*(60 if match[2]=='minute' else 1)
        if duration>p['maxDurationSeconds']: p['maxDurationSeconds']=duration
        p['targetDurationSeconds']=duration
    count=re.search(r'\b(\d+|two|three|four|five)\s+(?:vertical\s+|horizontal\s+)?highlight\s+clips?\b',t)
    count=(words.get(count[1],int(count[1]) if count[1].isdigit() else 1) if count else 1)
    if not 1<=count<=10: raise ValueError('Request between one and ten output variants.')
    return {'profiles':[validate_profile({**p,'id':f"{p['id']}-{i+1}"}) for i in range(count)],'subjectMode':'keep_all' if 'both speakers' in t else 'primary','layout':'stacked' if 'gameplay above' in t and ('face camera' in t or 'facecam' in t) else 'single','requiresReview':True}
