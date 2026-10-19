"""Transactional, source-referenced editing. No command opens or writes source media."""
from __future__ import annotations
from copy import deepcopy
import math
import json
import uuid
from .persistence import ProjectError, _connect, _json, _now

VERSION = 2
SUGGESTION_CATEGORIES = {"long_silence", "false_start", "repeated_take", "visual_quality", "pacing", "unmet_requirement", "punch_in"}

def _default_policies(): return {"protectedRanges": [], "suppressedCategories": []}

def _migrate_document(saved):
    saved=deepcopy(saved)
    if saved.get("schemaVersion")==1:
        saved["schemaVersion"]=VERSION; saved["policies"]=_default_policies()
        for stack in ("undo","redo"):
            for entry in saved.get(stack,[]):
                for key in ("before","after"):
                    entry[key]["policies"]=_default_policies()
    return saved
LIMIT = 50
PROPERTY_FIELDS={'role','userSupplied','orderSuggestionId','audioGainDb','linkedAudio','fadeInTicks','fadeOutTicks','videoFadeInTicks','videoFadeOutTicks','cropPreset','originSuggestionId','proposalRationale','proposalConfidence','punchInSuggestionIds','sourceProvenance','planId','sectionId','candidateId','sectionLabel','modelProvenance','linkedClipId','text','generatedBy','generatorVersion','transcriptId'}

def _id(): return str(uuid.uuid4())
def _track_kind(clip): return 'audio' if clip['itemType']=='audio' else 'caption' if clip['itemType']=='caption' else 'video'
def _provenance(clips,state):
    assets={a['id']:a for a in state['assets']}
    for c in clips:
        c.setdefault('properties',{}).setdefault('sourceProvenance',{'assetId':c['assetId'],'sha256':assets.get(c['assetId'],{}).get('sha256'),'origin':'legacy'})
def _clamp_fades(clip):
    props=clip['properties']; duration=clip['timelineDurationTicks']
    for key in ('fadeInTicks','fadeOutTicks','videoFadeInTicks','videoFadeOutTicks'): props[key]=min(props.get(key,0),duration//2)
def _integer(value, name, minimum=0):
    if type(value) is not int or value < minimum or value > 2**53-1:
        raise ValueError(f'{name} must be an integer in the supported range.')
    return value

def _base(state):
    saved=state['settings'].get('timelineEngine')
    if saved is not None:
        if not isinstance(saved,dict) or type(saved.get('schemaVersion')) is not int or saved.get('schemaVersion') not in (1,VERSION): raise ValueError('Unsupported timeline schema version.')
        try: saved=_migrate_document(saved)
        except (KeyError,TypeError,AttributeError): raise ValueError('Malformed legacy timeline history; restore a verified checkpoint.') from None
        _validate(saved,state)
        result=deepcopy(saved)
        # Relational items remain the compatibility projection. Refuse silently divergent state.
        keyed=lambda values: sorted(values,key=lambda value:value['id'])
        if keyed(result['clips']) != keyed(state['timelineItems']) or keyed(result['captions']) != keyed(state.get('captions',[])):
            result['clips']=deepcopy(state['timelineItems']); result['captions']=deepcopy(state.get('captions',[]))
            result['undo']=[]; result['redo']=[]
            known={t['id'] for t in result['tracks']}
            for clip in result['clips']:
                if clip['trackId'] not in known:
                    result['tracks'].append({'id':clip['trackId'],'kind':_track_kind(clip),'name':clip['trackId']}); known.add(clip['trackId'])
            _provenance(result['clips'],state)
        return result
    tracks=[]
    for clip in state['timelineItems']:
        if not any(t['id']==clip['trackId'] for t in tracks):
            tracks.append({'id':clip['trackId'],'kind':_track_kind(clip),'name':clip['trackId']})
    if not tracks: tracks=[{'id':'video','kind':'video','name':'Video'},{'id':'audio','kind':'audio','name':'Audio'}]
    result={'schemaVersion':VERSION,'tracks':tracks,'clips':deepcopy(state['timelineItems']),'captions':deepcopy(state.get('captions',[])),'markers':[],'undo':[],'redo':[],'policies':_default_policies()}
    _provenance(result['clips'],state)
    return result

def _snapshot(doc): return {k:deepcopy(doc[k]) for k in ('tracks','clips','captions','markers','policies')}

def _validate(doc,state):
    try: _validate_document(doc,state)
    except (KeyError,TypeError,AttributeError,OverflowError) as exc: raise ValueError('Malformed timeline document. Restore a verified project checkpoint.') from exc

def _validate_document(doc,state):
    if not isinstance(doc,dict) or set(doc)!={'schemaVersion','tracks','clips','captions','markers','undo','redo','policies'} or type(doc['schemaVersion']) is not int or doc['schemaVersion']!=VERSION: raise ValueError('Invalid timeline document.')
    if any(not isinstance(doc[k],list) for k in ('tracks','clips','captions','markers','undo','redo')): raise ValueError('Invalid timeline collections.')
    for key in ('undo','redo'):
        if len(doc[key])>LIMIT: raise ValueError('Timeline history exceeds supported retention.')
        for entry in doc[key]:
            if not isinstance(entry,dict) or set(entry)!={'command','before','after'} or not isinstance(entry['command'],dict): raise ValueError('Invalid reversible timeline history.')
            for snap in ('before','after'):
                if not isinstance(entry[snap],dict) or set(entry[snap])!={'tracks','clips','captions','markers','policies'}: raise ValueError('Invalid history snapshot.')
    policies=doc['policies']
    if not isinstance(policies,dict) or set(policies)!={'protectedRanges','suppressedCategories'} or not isinstance(policies['protectedRanges'],list) or not isinstance(policies['suppressedCategories'],list): raise ValueError('Invalid revision policies.')
    if any(not isinstance(category,str) or category not in SUGGESTION_CATEGORIES for category in policies['suppressedCategories']) or len(set(policies['suppressedCategories']))!=len(policies['suppressedCategories']): raise ValueError('Unknown suppressed suggestion category.')
    assets={a['id']:a for a in state['assets']}; tracks={}; ids=set(); intervals={}
    def unique(value):
        if not isinstance(value,str) or not value or len(value)>200 or value in ids: raise ValueError('Timeline IDs must be unique nonempty strings.')
        ids.add(value)
    for rule in policies['protectedRanges']:
        if not isinstance(rule,dict) or set(rule)!={'id','assetId','sourceInTicks','sourceOutTicks','phrase'}: raise ValueError('Invalid protected source range.')
        unique(rule['id']); asset=assets.get(rule['assetId'])
        a=_integer(rule['sourceInTicks'],'Protected source in'); b=_integer(rule['sourceOutTicks'],'Protected source out',1)
        if not asset or type(asset.get('duration_ticks')) is not int or not 0<=a<b<=asset['duration_ticks'] or not isinstance(rule['phrase'],str) or not rule['phrase'].strip() or len(rule['phrase'])>300: raise ValueError('Protected phrase requires valid source bounds.')
    for track in doc['tracks']:
        unique(track['id'])
        if set(track)!={'id','kind','name'} or track['kind'] not in ('video','audio','caption') or not isinstance(track['name'],str): raise ValueError('Invalid timeline track.')
        tracks[track['id']]=track
    for clip in doc['clips']:
        unique(clip['id']); asset=assets.get(clip['assetId']); track=tracks.get(clip['trackId'])
        if not asset or not track: raise ValueError('Clip references an unknown source or track.')
        if clip['itemType'] not in ('clip','audio','caption','overlay'): raise ValueError('Unsupported timeline item.')
        if clip['itemType']=='audio' and track['kind']!='audio': raise ValueError('Audio clips require an audio track.')
        if clip['itemType']=='clip' and track['kind']!='video': raise ValueError('Video clips require a video track.')
        if set(clip)-{'id','assetId','itemType','trackId','timelineStartTicks','timelineDurationTicks','sourceInTicks','sourceOutTicks','properties'}: raise ValueError('Unknown clip fields.')
        start=_integer(clip['timelineStartTicks'],'Timeline start'); duration=_integer(clip['timelineDurationTicks'],'Duration',1)
        _integer(start+duration,'Timeline end')
        a=_integer(clip['sourceInTicks'],'Source in'); b=_integer(clip['sourceOutTicks'],'Source out',1)
        if b<=a or duration!=b-a: raise ValueError('Only positive, source-accurate 1x clips are supported.')
        if asset.get('duration_ticks') is None or b>asset['duration_ticks']: raise ValueError('Clip exceeds known source bounds.')
        props=clip.get('properties',{})
        if not isinstance(props,dict): raise ValueError('Clip properties must be an object.')
        if set(props)-PROPERTY_FIELDS: raise ValueError('Unknown clip properties. Migrate the timeline before editing.')
        gain=props.get('audioGainDb',0)
        if type(gain) not in (float,int) or not math.isfinite(gain) or not -60<=gain<=24: raise ValueError('Audio gain must be between -60 and 24 dB.')
        provenance=props.get('sourceProvenance')
        if not isinstance(provenance,dict) or provenance.get('assetId')!=clip['assetId']: raise ValueError('Source provenance is required.')
        if set(provenance)-{'assetId','sha256','origin'} or provenance.get('origin') not in ('legacy','manual'): raise ValueError('Invalid source provenance.')
        sha=provenance.get('sha256')
        if sha is not None and (not isinstance(sha,str) or len(sha)!=64 or any(c not in '0123456789abcdef' for c in sha)): raise ValueError('Invalid source fingerprint.')
        for name in ('fadeInTicks','fadeOutTicks','videoFadeInTicks','videoFadeOutTicks'):
            if _integer(props.get(name,0),name)>duration: raise ValueError('Fade exceeds clip duration.')
        if props.get('fadeInTicks',0)+props.get('fadeOutTicks',0)>duration or props.get('videoFadeInTicks',0)+props.get('videoFadeOutTicks',0)>duration: raise ValueError('Clip fades overlap.')
        if clip['itemType'] in ('clip','audio'): intervals.setdefault(clip['trackId'],[]).append((start,start+duration))
    for values in intervals.values():
        ordered=sorted(values)
        if any(a[1]>b[0] for a,b in zip(ordered,ordered[1:])): raise ValueError('Clips overlap on the same track.')
    clip_ids={c['id'] for c in doc['clips']}
    caption_items={c['id']:c for c in doc['clips'] if c['itemType']=='caption'}
    records={}
    for caption in doc['captions']:
        unique(caption['id'])
        if set(caption)!={'id','timelineItemId','text','language','style'} or not isinstance(caption['language'],str) or not isinstance(caption['style'],dict): raise ValueError('Invalid caption record.')
        if caption['timelineItemId'] not in caption_items or not isinstance(caption['text'],str) or not caption['text'].strip() or len(caption['text'])>4000: raise ValueError('Caption requires text and a source-referenced caption item.')
        if caption['timelineItemId'] in records: raise ValueError('Duplicate caption text record.')
        records[caption['timelineItemId']]=caption
    if set(records)!=set(caption_items): raise ValueError('Every caption item requires exactly one text record.')
    for marker in doc['markers']:
        unique(marker['id']); _integer(marker['atTicks'],'Marker time')
        if not isinstance(marker['label'],str) or len(marker['label'])>1000: raise ValueError('Invalid marker label.')
    for c in caption_items.values():
        parent_id=c.get('properties',{}).get('linkedClipId')
        parent=next((p for p in doc['clips'] if p['id']==parent_id and p['itemType'] in ('clip','audio')),None)
        if parent is None or c['assetId']!=parent['assetId'] or not parent['sourceInTicks']<=c['sourceInTicks']<c['sourceOutTicks']<=parent['sourceOutTicks']: raise ValueError('Caption must reference its parent source range.')
        if tracks[c['trackId']]['kind']!='caption' or c['timelineStartTicks']!=parent['timelineStartTicks']+c['sourceInTicks']-parent['sourceInTicks']: raise ValueError('Caption placement must follow its source clip.')
        if c.get('properties',{}).get('text')!=records[c['id']]['text']: raise ValueError('Caption text copies disagree.')
    _json(doc) # Reject unsupported JSON values before any write.

def _public(doc,revision):
    result=_snapshot(doc)
    result.update(schemaVersion=VERSION,revision=revision,durationTicks=max((c['timelineStartTicks']+c['timelineDurationTicks'] for c in doc['clips']),default=0),canUndo=bool(doc['undo']),canRedo=bool(doc['redo']))
    return result

def timeline_state(store):
    state=store.load_state(); doc=_base(state); _validate(doc,state)
    return _public(doc,state['revision'])

def _persist(store,connection,state,doc):
    _validate(doc,state)
    state['timelineItems']=doc['clips']; state['captions']=doc['captions']; state['settings']['timelineEngine']=doc
    store._validate_state(connection,store.project_id,state)
    store._replace_state(connection,store.project_id,state)
    revision=state['revision']+1
    connection.execute('UPDATE projects SET state_revision=?,updated_at=? WHERE id=?',(revision,_now(),store.project_id))
    state['revision']=revision
    store._write_checkpoint_locked(connection,store.project_id,revision,state)
    return _public(doc,revision)

def _transaction(store,expected_revision,mutate,commit_hook=None):
    _integer(expected_revision,'Expected revision')
    with _connect(store.database_path) as connection:
        connection.execute('BEGIN IMMEDIATE'); state=store.load_state(connection)
        if state['revision']!=expected_revision: raise ProjectError('The timeline changed. Refresh before editing.')
        doc=_base(state); _validate(doc,state); before=_snapshot(doc); mutate(doc,state)
        if _snapshot(doc)==before and commit_hook is None: return _public(doc,state['revision'])
        if commit_hook is not None: commit_hook(connection,state,doc)
        result=_persist(store,connection,state,doc); connection.commit()
    store._checkpoint_if_due(result['revision'])
    return result

def _shift(doc,track,at,delta,exclude=()):
    for item in doc['clips']:
        if item['id'] not in exclude and item['trackId']==track and item['timelineStartTicks']>=at:
            item['timelineStartTicks']+=delta
    # Captions follow their parent placement, independently of their caption track.
    parents={c['id']:c for c in doc['clips']}
    for item in doc['clips']:
        parent=parents.get(item.get('properties',{}).get('linkedClipId'))
        if parent: item['timelineStartTicks']=parent['timelineStartTicks']+item['sourceInTicks']-parent['sourceInTicks']

def _repair_captions(doc):
    parents={c['id']:c for c in doc['clips'] if c['itemType'] in ('clip','audio')}; remove=set()
    for c in doc['clips']:
        if c['itemType']!='caption': continue
        parent=parents.get(c.get('properties',{}).get('linkedClipId'))
        if not parent: remove.add(c['id']); continue
        a=max(c['sourceInTicks'],parent['sourceInTicks']); b=min(c['sourceOutTicks'],parent['sourceOutTicks'])
        if b<=a: remove.add(c['id']); continue
        c.update(sourceInTicks=a,sourceOutTicks=b,timelineDurationTicks=b-a,timelineStartTicks=parent['timelineStartTicks']+a-parent['sourceInTicks'])
    doc['clips']=[c for c in doc['clips'] if c['id'] not in remove]
    doc['captions']=[c for c in doc['captions'] if c['timelineItemId'] not in remove]
    markers=[]
    for marker in doc['markers']:
        if 'planId' in marker and 'sectionId' in marker:
            members=[c for c in doc['clips'] if c['itemType']=='clip' and c.get('properties',{}).get('planId')==marker['planId'] and c.get('properties',{}).get('sectionId')==marker['sectionId']]
            if not members: continue
            marker['atTicks']=min(c['timelineStartTicks'] for c in members)
        markers.append(marker)
    doc['markers']=markers

def _assert_unprotected(doc,asset_id,start,end):
    if any(r['assetId']==asset_id and start<r['sourceOutTicks'] and end>r['sourceInTicks'] for r in doc['policies']['protectedRanges']):
        raise ValueError('This edit would remove a protected source mention. Keep it or undo the protection first.')

def _remove_range(doc,state,clip,start,end,ripple):
    _integer(start,'Removal source in'); _integer(end,'Removal source out',1)
    if not clip['sourceInTicks']<=start<end<=clip['sourceOutTicks']: raise ValueError('Removal must lie inside the selected source clip.')
    _assert_unprotected(doc,clip['assetId'],start,end)
    timeline_start=clip['timelineStartTicks']+start-clip['sourceInTicks']
    if ripple and any(c['itemType'] in ('clip','audio') and c['trackId']!=clip['trackId'] and c['timelineStartTicks']+c['timelineDurationTicks']>timeline_start for c in doc['clips']):
        raise ValueError('Ripple removal would desynchronize independent tracks. Use a gap-preserving removal or edit those tracks explicitly.')
    middle=clip
    if start>clip['sourceInTicks']:
        old_ids={c['id'] for c in doc['clips']}
        _apply(doc,state,{'type':'split','clipId':clip['id'],'atTicks':timeline_start})
        middle=next(c for c in doc['clips'] if c['id'] not in old_ids and c['itemType']==clip['itemType'])
    if end<middle['sourceOutTicks']:
        _apply(doc,state,{'type':'split','clipId':middle['id'],'atTicks':middle['timelineStartTicks']+end-middle['sourceInTicks']})
    _apply(doc,state,{'type':'delete','clipId':middle['id'],'ripple':ripple})

COMMAND_FIELDS={
 'remove_range':({'clipId','sourceInTicks','sourceOutTicks','ripple'},set()),
 'crop':({'clipId','cropPreset'},set()),
 'protect_ranges':({'ranges'},set()),
 'suppress_category':({'category'},set()),
 'insert':({'assetId','trackId','sourceInTicks','sourceOutTicks'},{'atTicks','ripple'}),
 'trim':({'clipId','sourceInTicks','sourceOutTicks'},{'ripple'}),'split':({'clipId','atTicks'},set()),
 'delete':({'clipId'},{'ripple'}),'move':({'clipId','atTicks'},{'trackId'}),'reorder':({'trackId','clipIds'},set()),
 'gap':({'trackId','atTicks','durationTicks'},set()),'ripple':({'trackId','atTicks','deltaTicks'},set()),
 'gain':({'clipId','gainDb'},set()),'fades':({'clipId','fadeInTicks','fadeOutTicks'},set()),
 'caption':({'clipId','text'},{'sourceInTicks','sourceOutTicks'}),'marker':({'atTicks','label'},{'markerId'}),
 'edit_caption':({'captionId','text'},set()),
 'remove_marker':({'markerId'},set()),'track':({'kind','name'},set())}

def _apply(doc,state,command):
    if not isinstance(command,dict) or command.get('type') not in COMMAND_FIELDS: raise ValueError('Unknown timeline command.')
    kind=command['type']; required,optional=COMMAND_FIELDS[kind]
    if not required<=command.keys() or set(command)-required-optional-{'type'}: raise ValueError('Invalid command fields.')
    if 'ripple' in command and type(command['ripple']) is not bool: raise ValueError('Ripple must be boolean.')
    clip=None
    if 'clipId' in required:
        allowed=('clip','audio','caption') if kind=='delete' else ('clip','audio')
        clip=next((c for c in doc['clips'] if c['id']==command['clipId'] and c['itemType'] in allowed),None)
        if clip is None: raise ValueError('Select an existing source clip.')
    track=command.get('trackId',clip['trackId'] if clip else None)
    if track is not None and not any(t['id']==track for t in doc['tracks']): raise ValueError('Unknown track.')
    if kind=='remove_range': _remove_range(doc,state,clip,command['sourceInTicks'],command['sourceOutTicks'],command['ripple'])
    elif kind=='crop':
        if clip['itemType']!='clip' or command['cropPreset'] not in ('full_frame','punch_in_110','punch_in_125'): raise ValueError('Choose a supported video crop preset.')
        clip['properties']['cropPreset']=command['cropPreset']
    elif kind=='protect_ranges':
        if not isinstance(command['ranges'],list) or not command['ranges'] or len(command['ranges'])>1000: raise ValueError('Provide bounded protected source ranges.')
        for rule in command['ranges']:
            if not isinstance(rule,dict) or set(rule)!={'assetId','sourceInTicks','sourceOutTicks','phrase'}: raise ValueError('Invalid protected range fields.')
            if not any(all(r[k]==v for k,v in rule.items()) for r in doc['policies']['protectedRanges']): doc['policies']['protectedRanges'].append({'id':_id(),**deepcopy(rule)})
    elif kind=='suppress_category':
        if not isinstance(command['category'],str) or command['category'] not in SUGGESTION_CATEGORIES: raise ValueError('Unknown suggestion category.')
        if command['category'] not in doc['policies']['suppressedCategories']: doc['policies']['suppressedCategories'].append(command['category'])
    elif kind=='track': doc['tracks'].append({'id':_id(),'kind':command['kind'],'name':command['name']})
    elif kind=='insert':
        a=_integer(command['sourceInTicks'],'Source in'); b=_integer(command['sourceOutTicks'],'Source out',1)
        at=_integer(command.get('atTicks',max((c['timelineStartTicks']+c['timelineDurationTicks'] for c in doc['clips'] if c['trackId']==track),default=0)),'Insert time')
        if command.get('ripple'): _shift(doc,track,at,b-a)
        asset=next((a for a in state['assets'] if a['id']==command['assetId']),None)
        doc['clips'].append({'id':_id(),'assetId':command['assetId'],'itemType':'audio' if next(t for t in doc['tracks'] if t['id']==track)['kind']=='audio' else 'clip','trackId':track,'timelineStartTicks':at,'timelineDurationTicks':b-a,'sourceInTicks':a,'sourceOutTicks':b,'properties':{'audioGainDb':0,'linkedAudio':True,'sourceProvenance':{'assetId':command['assetId'],'sha256':asset.get('sha256') if asset else None,'origin':'manual'}}})
    elif kind=='trim':
        old_end=clip['timelineStartTicks']+clip['timelineDurationTicks']; old=clip['timelineDurationTicks']
        a=_integer(command['sourceInTicks'],'Source in'); b=_integer(command['sourceOutTicks'],'Source out',1)
        if a>clip['sourceInTicks']: _assert_unprotected(doc,clip['assetId'],clip['sourceInTicks'],min(a,clip['sourceOutTicks']))
        if b<clip['sourceOutTicks']: _assert_unprotected(doc,clip['assetId'],max(b,clip['sourceInTicks']),clip['sourceOutTicks'])
        clip.update(sourceInTicks=a,sourceOutTicks=b,timelineDurationTicks=b-a)
        _clamp_fades(clip)
        if command.get('ripple'): _shift(doc,track,old_end,b-a-old,(clip['id'],))
    elif kind=='split':
        at=_integer(command['atTicks'],'Split time'); offset=at-clip['timelineStartTicks']
        if not 0<offset<clip['timelineDurationTicks']: raise ValueError('Split must be inside the clip.')
        right=deepcopy(clip); right.update(id=_id(),sourceInTicks=clip['sourceInTicks']+offset,timelineStartTicks=at,timelineDurationTicks=clip['timelineDurationTicks']-offset)
        clip.update(sourceOutTicks=right['sourceInTicks'],timelineDurationTicks=offset)
        for c in list(doc['clips']):
            if c.get('properties',{}).get('linkedClipId')==clip['id'] and c['sourceOutTicks']>right['sourceInTicks']:
                new=deepcopy(c); new['id']=_id(); new['properties']['linkedClipId']=right['id']; doc['clips'].append(new)
                for record in list(doc['captions']):
                    if record['timelineItemId']==c['id']: doc['captions'].append({**record,'id':_id(),'timelineItemId':new['id']})
        doc['clips'].append(right)
        for c in (clip,right): c['properties'].update(fadeInTicks=0,fadeOutTicks=0,videoFadeInTicks=0,videoFadeOutTicks=0)
    elif kind=='delete':
        if clip['itemType']=='caption' and command.get('ripple'): raise ValueError('Caption deletion cannot ripple source tracks.')
        if clip['itemType']!='caption': _assert_unprotected(doc,clip['assetId'],clip['sourceInTicks'],clip['sourceOutTicks'])
        doc['clips'].remove(clip)
        doc['captions']=[c for c in doc['captions'] if c['timelineItemId']!=clip['id']]
        if command.get('ripple'): _shift(doc,track,clip['timelineStartTicks']+clip['timelineDurationTicks'],-clip['timelineDurationTicks'])
    elif kind=='move': clip.update(timelineStartTicks=_integer(command['atTicks'],'Move time'),trackId=track)
    elif kind=='reorder':
        clips=[c for c in doc['clips'] if c['trackId']==track and c['itemType'] in ('clip','audio')]; order=command['clipIds']
        if not isinstance(order,list) or len(order)!=len(clips) or set(order)!={c['id'] for c in clips}: raise ValueError('Reorder must include every track clip exactly once.')
        by_id={c['id']:c for c in clips}; originals=sorted(clips,key=lambda c:c['timelineStartTicks']); previous=0; gaps=[]
        for c in originals: gaps.append(c['timelineStartTicks']-previous); previous=c['timelineStartTicks']+c['timelineDurationTicks']
        at=0
        for index,cid in enumerate(order): at+=gaps[index]; by_id[cid]['timelineStartTicks']=at; at+=by_id[cid]['timelineDurationTicks']
    elif kind in ('gap','ripple'):
        at=_integer(command['atTicks'],'Ripple time')
        delta=_integer(command['durationTicks'],'Gap duration',1) if kind=='gap' else command['deltaTicks']
        if type(delta) is not int or abs(delta)>2**53-1: raise ValueError('Ripple delta must be an integer.')
        if any(c['trackId']==track and c['timelineStartTicks']<at<c['timelineStartTicks']+c['timelineDurationTicks'] for c in doc['clips'] if c['itemType'] in ('clip','audio')): raise ValueError('Split clips before rippling through them.')
        _shift(doc,track,at,delta)
    elif kind=='gain': clip['properties']['audioGainDb']=command['gainDb']
    elif kind=='fades': clip['properties'].update(fadeInTicks=command['fadeInTicks'],fadeOutTicks=command['fadeOutTicks'],videoFadeInTicks=command['fadeInTicks'],videoFadeOutTicks=command['fadeOutTicks'])
    elif kind=='caption':
        if not isinstance(command['text'],str) or not command['text'].strip() or len(command['text'])>4000: raise ValueError('Caption must contain 1-4000 characters.')
        a=command.get('sourceInTicks',clip['sourceInTicks']); b=command.get('sourceOutTicks',clip['sourceOutTicks'])
        _integer(a,'Caption in'); _integer(b,'Caption out',1)
        if not clip['sourceInTicks']<=a<b<=clip['sourceOutTicks']: raise ValueError('Caption must be inside its source clip.')
        caption_track=next((t['id'] for t in doc['tracks'] if t['kind']=='caption'),None)
        if caption_track is None: caption_track=_id(); doc['tracks'].append({'id':caption_track,'kind':'caption','name':'Captions'})
        cid=_id(); doc['clips'].append({'id':cid,'assetId':clip['assetId'],'itemType':'caption','trackId':caption_track,'timelineStartTicks':clip['timelineStartTicks']+a-clip['sourceInTicks'],'timelineDurationTicks':b-a,'sourceInTicks':a,'sourceOutTicks':b,'properties':{'linkedClipId':clip['id'],'text':command['text'],'sourceProvenance':deepcopy(clip['properties'].get('sourceProvenance',{}))}})
        doc['captions'].append({'id':_id(),'timelineItemId':cid,'text':command['text'],'language':'und','style':{}})
    elif kind=='edit_caption':
        if not isinstance(command['text'],str) or not command['text'].strip() or len(command['text'])>4000: raise ValueError('Caption must contain 1-4000 characters.')
        record=next((c for c in doc['captions'] if c['id']==command['captionId'] or c['timelineItemId']==command['captionId']),None)
        if record is None: raise ValueError('Caption was not found.')
        item=next(c for c in doc['clips'] if c['id']==record['timelineItemId'])
        record['text']=command['text']; item['properties']['text']=command['text']
    elif kind=='marker':
        marker=next((m for m in doc['markers'] if m['id']==command.get('markerId')),None)
        if command.get('markerId') and marker is None: raise ValueError('Marker was not found.')
        if marker is None: marker={'id':_id()}; doc['markers'].append(marker)
        marker.update(atTicks=command['atTicks'],label=command['label'])
    elif kind=='remove_marker':
        marker=next((m for m in doc['markers'] if m['id']==command['markerId']),None)
        if marker is None: raise ValueError('Marker was not found.')
        doc['markers'].remove(marker)
    _repair_captions(doc)

def timeline_command(store,command,expected_revision):
    def mutate(doc,state):
        before=_snapshot(doc)
        try: _apply(doc,state,command)
        except (KeyError,TypeError,AttributeError,OverflowError) as exc: raise ValueError('Malformed timeline command.') from exc
        _validate(doc,state)
        doc['undo']=(doc['undo']+[{'command':deepcopy(command),'before':before,'after':_snapshot(doc)}])[-LIMIT:]; doc['redo']=[]
    return _transaction(store,expected_revision,mutate)

def _history(store,expected_revision,undo):
    moved={}
    def mutate(doc,state):
        source='undo' if undo else 'redo'; target='redo' if undo else 'undo'
        if not doc[source]: raise ValueError('No edit available to '+source+'.')
        entry=doc[source][-1]; expected=entry['after' if undo else 'before']
        if _snapshot(doc)!=expected: raise ProjectError('Timeline history diverged; refresh the project.')
        moved['entry']=entry
        doc[source].pop(); doc.update(deepcopy(entry['before' if undo else 'after'])); doc[target].append(entry)
    def commit_hook(connection,state,doc):
        entry=moved['entry']; command=entry['command']
        if command.get('type')!='revision_accept': return
        from . import revisions
        row=connection.execute('SELECT * FROM edit_plans WHERE id=? AND project_id=?',(command.get('suggestionId'),store.project_id)).fetchone()
        if row is None: raise ProjectError('The suggestion history record is missing.')
        plan=json.loads(row['plan_json']); expected_status='accepted' if undo else 'undone'
        if plan.get('planType')!='timeline-revision' or row['status']!=expected_status: raise ProjectError('Suggestion history status changed. Refresh the project.')
        if entry['before']!=plan.get('before') or entry['after']!=plan.get('after'): raise ProjectError('Suggestion history and its reviewed diff disagree.')
        ctx=revisions._context(store,True)
        if ctx['fingerprint']!=plan['evidenceFingerprint']: raise ProjectError('Suggestion evidence changed. Review the changed constraints before undo or redo.')
        if undo or plan.get('restore'):
            previous=deepcopy(doc); previous.update(deepcopy(entry['after' if undo else 'before']))
            revisions._validate_restoration(doc,ctx,previous)
        else:
            previous=deepcopy(doc); previous.update(deepcopy(entry['before']))
            after=revisions._simulate(previous,state,ctx,plan['operations'])
            if revisions._canonical(_snapshot(after),entry['before'])!=revisions._canonical(entry['after'],entry['before']): raise ProjectError('The suggestion diff no longer matches its operations.')
        status='undone' if undo else 'accepted'; plan['status']=status
        connection.execute('UPDATE edit_plans SET status=?,plan_json=? WHERE id=? AND project_id=?',(status,_json(plan),row['id'],store.project_id))
    return _transaction(store,expected_revision,mutate,commit_hook=commit_hook)

def timeline_undo(store,expected_revision): return _history(store,expected_revision,True)
def timeline_redo(store,expected_revision): return _history(store,expected_revision,False)

def compile_plan(store,plan_id,section_ids,expected_review_version,expected_revision,runtime=None):
    from .plan_review import _evidence
    from .planning import validate_written_plan
    import json
    if not isinstance(plan_id,str) or not plan_id: raise ValueError('Select a written plan.')
    _integer(expected_review_version,'Expected review version')
    if not isinstance(section_ids,list) or not section_ids or any(not isinstance(s,str) or not s for s in section_ids) or len(section_ids)!=len(set(section_ids)): raise ValueError('Select approved sections exactly once.')
    def mutate(doc,state):
        # This runs under the same write transaction as timeline publication.
        with _connect(store.database_path) as reader:
            row=reader.execute('SELECT plan_json FROM edit_plans WHERE id=? AND project_id=?',(plan_id,store.project_id)).fetchone()
        if row is None: raise ValueError('Written plan was not found.')
        plan=json.loads(row[0])
        if plan.get('reviewVersion')!=expected_review_version: raise ProjectError('The written plan changed. Refresh before compiling.')
        evidence=_evidence(store,plan,verify_sources=True,runtime=runtime)
        if evidence['fingerprint']!=plan.get('evidenceFingerprint'): raise ProjectError('Planning evidence is stale. Generate a fresh plan.')
        validate_written_plan(plan,evidence)
        if not isinstance(section_ids,list) or not section_ids or len(section_ids)!=len(set(section_ids)): raise ValueError('Select approved sections exactly once.')
        selected=[s for s in plan['sections'] if s['id'] in section_ids]
        if len(selected)!=len(section_ids) or any(s['status']!='approved' for s in selected): raise ValueError('Only explicitly approved sections may be compiled.')
        before=_snapshot(doc); candidates={c['id']:c for c in evidence['candidates']}
        track=next((t['id'] for t in doc['tracks'] if t['kind']=='video'),None)
        if track is None: track=_id(); doc['tracks'].append({'id':track,'kind':'video','name':'Video'})
        for section in selected:
            for selection in section['selections']:
                cid=selection['candidateId']; candidate=candidates[cid]
                if any(c.get('properties',{}).get('planId')==plan_id and c.get('properties',{}).get('sectionId')==section['id'] and c.get('properties',{}).get('candidateId')==cid for c in doc['clips']): continue
                at=max((c['timelineStartTicks']+c['timelineDurationTicks'] for c in doc['clips'] if c['trackId']==track),default=0)
                _apply(doc,state,{'type':'insert','trackId':track,'assetId':candidate['assetId'],'sourceInTicks':candidate['sourceInTicks'],'sourceOutTicks':candidate['sourceOutTicks'],'atTicks':at})
                doc['clips'][-1]['properties'].update(planId=plan_id,sectionId=section['id'],candidateId=cid,sectionLabel=section['title'],modelProvenance=deepcopy(plan['provenance']))
                if not any(m.get('sectionId')==section['id'] and m.get('planId')==plan_id for m in doc['markers']): doc['markers'].append({'id':_id(),'atTicks':at,'label':section['title'],'sectionId':section['id'],'planId':plan_id})
        _validate(doc,state); doc['undo']=(doc['undo']+[{'command':{'type':'compile_plan','planId':plan_id,'sectionIds':section_ids},'before':before,'after':_snapshot(doc)}])[-LIMIT:]; doc['redo']=[]
    return _transaction(store,expected_revision,mutate)
