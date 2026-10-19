"""Evidence-bound review proposals. Content is never interpreted as executable instructions."""
from copy import deepcopy
import hashlib
import json
import re
from . import timeline as tl
from .footage_index import _snapshot as source_snapshot
from .analysis import DETECTORS, _transcript_review_fingerprint
from .persistence import _connect, _json, _sha256_file, ProjectError

CATEGORIES={'long_silence','false_start','repeated_take','visual_quality','pacing','unmet_requirement','protect_topic','restore','punch_in'}

def _hash(value): return hashlib.sha256(_json(value).encode()).hexdigest()
def _norm(value): return ' '.join(re.findall(r"[\w']+",str(value).casefold()))
def _overlap(a,b): return a['assetId']==b['assetId'] and a['sourceInTicks']<b['sourceOutTicks'] and b['sourceInTicks']<a['sourceOutTicks']

def parse_revision(text):
    if not isinstance(text,str) or not 1<=len(text.strip())<=500: raise ValueError('Enter one supported editing request, up to 500 characters.')
    value=text.strip().rstrip('.').strip()
    fixed={ 'tighten pauses in the first minute':{'intent':'tighten','endTicks':60_000_000},'make the opening faster':{'intent':'opening'},'restore the longer explanation':{'intent':'restore'},'remove this suggestion everywhere':{'intent':'suppress'},'use fewer punch-ins':{'intent':'punch_in'}}
    if value.casefold() in fixed: return fixed[value.casefold()]
    match=re.fullmatch(r'keep every mention of ([\w][\w \-]{0,79})',value,re.I)
    if match:
        phrase=match.group(1).strip()
        if re.search(r'\b(and|then|ignore|execute|system|instructions|ffmpeg|delete|run)\b',phrase,re.I): raise ValueError('Enter one literal topic without additional instructions.')
        return {'intent':'protect','phrase':phrase}
    raise ValueError('Unsupported or ambiguous request. Use one supported command; select a suggestion before restore or dismiss.')

def _context(store,verify=False):
    data,base=source_snapshot(store,verify_sources=verify)
    assets={a['id']:a for a in data['assets']}; transcripts={t['assetId']:t for t in data['transcripts']}
    analyses=[]; latest={}
    for row in store.list_analyses():
        key=(row['assetId'],row['detectorId'])
        if row['detectorId'] not in DETECTORS or row['assetId'] not in assets or row['_artifactStatus']!='ready' or row['sourceFingerprint']!=assets[row['assetId']]['sha256'] or row['detectorVersion']!=DETECTORS[row['detectorId']]['version']: continue
        latest[key]=row['artifactId']
    checked={}
    for row in store.list_analyses():
        if (row['assetId'],row['detectorId']) not in latest or latest[(row['assetId'],row['detectorId'])]!=row['artifactId']: continue
        config=row['_artifactConfig']; transcript=transcripts.get(row['assetId'])
        if DETECTORS[row['detectorId']].get('needsTranscript') and (not transcript or config.get('transcriptId')!=transcript['id'] or config.get('transcriptReviewFingerprint')!=_transcript_review_fingerprint(transcript)): continue
        if row['artifactId'] not in checked:
            path=(store.root/row['_artifactPath']).resolve(); path.relative_to(store.root.resolve())
            if _sha256_file(path)!=row['_artifactChecksum']: raise ValueError('Analysis artifact failed integrity verification. Regenerate analysis.')
            try: payload=json.loads(path.read_text(encoding='utf-8'))
            except (OSError,ValueError): raise ValueError('Analysis artifact cannot be read. Regenerate analysis.') from None
            if not isinstance(payload,dict): raise ValueError('Invalid analysis evidence artifact.')
            checked[row['artifactId']]=payload
        payload=checked[row['artifactId']]
        if payload.get('assetId')!=row['assetId'] or payload.get('sourceFingerprint')!=row['sourceFingerprint'] or payload.get('detectorId')!=row['detectorId'] or payload.get('detectorVersion')!=row['detectorVersion'] or not isinstance(payload.get('results'),list) or sum(result==row['result'] for result in payload['results'])!=1:
            raise ValueError('Indexed analysis does not match its verified artifact. Regenerate analysis.')
        a,b=row['sourceInTicks'],row['sourceOutTicks']
        if type(a) is not int or type(b) is not int or not 0<=a<b<=assets[row['assetId']]['duration_ticks']: continue
        analyses.append({k:row[k] for k in ('id','assetId','detectorId','detectorVersion','sourceInTicks','sourceOutTicks','result')})
    briefs=store.list_briefs(); plans=[r for r in store.list_edit_plans() if r['plan'].get('planType')=='written-rough-cut']
    context={'data':data,'analyses':analyses,'brief':briefs[0] if briefs else None,'plans':plans}
    context['fingerprint']=_hash({'source':base,'analyses':analyses,'briefs':briefs,'plans':plans})
    return context

def _literal_ranges(transcript,phrase):
    target=_norm(phrase); found=[]; words=transcript['words']
    if not target: return []
    for i in range(len(words)):
        for j in range(i,min(len(words),i+30)):
            span=words[i:j+1]
            if any(_norm(' '.join(w.get(field,w.get('text','')) for w in span))==target for field in ('text','originalText')):
                found.append({'assetId':transcript['assetId'],'sourceInTicks':span[0]['sourceInTicks'],'sourceOutTicks':span[-1]['sourceOutTicks'],'phrase':phrase}); break
    for segment in transcript['segments']:
        if any(' '+target+' ' in ' '+_norm(segment.get(field,''))+' ' for field in ('text','originalText')):
            found.append({'assetId':transcript['assetId'],'sourceInTicks':segment['sourceInTicks'],'sourceOutTicks':segment['sourceOutTicks'],'phrase':phrase})
    return found

def _protected(doc,ctx):
    result=list(doc['policies']['protectedRanges'])
    brief=ctx['brief']; terms=brief['constraints'].get('mustKeep',[]) if brief else []
    for t in ctx['data']['transcripts']:
        result.extend({'assetId':t['assetId'],**m} for m in t.get('markers',[]) if m['marker']=='required')
        for term in terms: result.extend(_literal_ranges(t,term))
    return result

def _locked(clip,ctx):
    p=clip.get('properties',{})
    return any(r['id']==p.get('planId') and any(s['id']==p.get('sectionId') and s['locked'] for s in r['plan']['sections']) for r in ctx['plans'])

def _guard(doc,ctx,commands):
    protected=_protected(doc,ctx)
    if not isinstance(commands,list) or len(commands)>1000: raise ValueError('Provide a bounded operation list.')
    for command in commands:
        if not isinstance(command,dict) or not isinstance(command.get('type'),str): raise ValueError('Malformed revision operation.')
        if command['type'] not in {'remove_range','crop','protect_ranges','suppress_category'}: raise ValueError('Unsupported revision operation.')
        clip=next((c for c in doc['clips'] if c['id']==command.get('clipId')),None)
        if clip and _locked(clip,ctx): raise ValueError('Unlock this approved plan section before revising its clips.')
        if command['type']=='remove_range':
            if clip is None: raise ValueError('The affected clip no longer exists.')
            span={'assetId':clip['assetId'],**command}
            if any(_overlap(span,p) for p in protected): raise ValueError('This edit intersects a required or protected source range.')
            if command['ripple'] and any(c['trackId']!=clip['trackId'] and c['itemType']=='audio' for c in doc['clips']): raise ValueError('Independent audio tracks require manual synchronized trimming; automatic ripple is unavailable.')

def _simulate(doc,state,ctx,commands):
    after=deepcopy(doc); _guard(after,ctx,commands)
    for command in commands: tl._apply(after,state,command)
    tl._validate(after,state)
    return after

def _duration(doc): return max((c['timelineStartTicks']+c['timelineDurationTicks'] for c in doc['clips']),default=0)
def _evidence(row): return {k:row[k] for k in ('assetId','sourceInTicks','sourceOutTicks','detectorId','detectorVersion')}|{'explanation':row['result'].get('explanation','Review against playback.'),'text':row['result'].get('text','')}

def _save(store,doc,state,ctx,kind,title,explanation,commands,evidence,start,end,warnings=None,restore=None):
    if any(r['status']=='pending' and r['plan']['baseRevision']==state['revision'] and r['plan']['evidenceFingerprint']==ctx['fingerprint'] and r['plan']['kind']==kind and r['plan']['operations']==commands and r['plan']['explanation']==explanation for r in _records(store)): return
    if len([r for r in _records(store) if r['status']=='pending' and r['plan']['baseRevision']==state['revision']])>=200: return
    after=_simulate(doc,state,ctx,commands) if restore is None else deepcopy(restore)
    if commands and tl._snapshot(after)==tl._snapshot(doc): raise ValueError('The requested change is already in effect. No new operation was proposed.')
    plan={'planType':'timeline-revision','schemaVersion':1,'kind':kind,'title':title,'status':'pending','baseRevision':state['revision'],'evidenceFingerprint':ctx['fingerprint'],'evidence':evidence,'timelineInTicks':start,'timelineOutTicks':end,'operations':commands,'durationDeltaTicks':_duration(after)-_duration(doc),'preview':{'before':deepcopy(doc['clips']),'after':deepcopy(after['clips'])},'warnings':warnings or [],'evidenceNotes':_evidence_notes(kind,ctx,commands,explanation),'explanation':explanation,'before':tl._snapshot(doc),'after':tl._snapshot(after)}
    if restore is not None: plan['restore']=True
    store.add_edit_plan(plan,status='pending',rationale=[],generator_id='localcut-revision-rules',generator_version='1')

def _evidence_notes(kind,ctx,commands,explanation):
    notes=[]
    if kind=='long_silence' and any(c['type']=='remove_range' for c in commands): notes.append('Measured silence of at least 0.8s; preserve 0.15s at each edge and exclude timed speech.')
    if kind in ('pacing','unmet_requirement') and ctx['brief']:
        brief=ctx['brief']; notes.append(f'Saved brief {brief["id"]}, revision {brief["revision"]}; target duration: {brief["constraints"].get("targetDurationMinutes")} minutes. '+explanation)
    if kind=='protect_topic': notes.append('Every exact original or reviewed transcript match is protected as a source range, including occurrences outside the current timeline.')
    if kind=='punch_in': notes.append(f'{len(commands)} existing punch-in crop(s) will change to full_frame; source timestamps and clip durations are unchanged.')
    if any(c['type']=='suppress_category' for c in commands): notes.append('Project policy only: suppress this category across the project. No footage, speech, or source file is deleted.')
    if kind=='restore': notes.append('Exact saved timeline before the selected accepted removal; restoration is allowed only while the current timeline matches its saved after state.')
    return notes or [explanation]

def _records(store): return [r for r in store.list_edit_plans() if r['plan'].get('planType')=='timeline-revision']
def _public(record,revision,fingerprint):
    plan=record['plan']; value={k:deepcopy(v) for k,v in plan.items() if k not in {'before','after','restore','planType','schemaVersion'}}
    value.update(id=record['id'],status=record['status'],stale=plan['baseRevision']!=revision or plan['evidenceFingerprint']!=fingerprint)
    operations=plan['operations']
    if plan.get('restore'): summary='Restore the exact timeline state before the selected removal.'
    elif not operations: summary='Manual review only; no automatic timeline change.'
    else:
        descriptions=[]
        for op in operations:
            if op['type']=='remove_range': descriptions.append(f"Remove source {op['sourceInTicks']/1e6:g}-{op['sourceOutTicks']/1e6:g}s from clip {op['clipId']}"+(' and close the gap.' if op['ripple'] else '; retain the placement of later footage.'))
            elif op['type']=='protect_ranges': descriptions.append(f"Protect {len(op['ranges'])} exact source mention range(s); do not restore missing footage automatically.")
            elif op['type']=='suppress_category': descriptions.append(f"Suppress {op['category']} suggestions throughout this project; preserve footage.")
            elif op['type']=='crop': descriptions.append(f"Set clip {op['clipId']} to {op['cropPreset']} framing.")
        summary=' '.join(descriptions)
    value['operationSummary']=summary
    value['staleReason']='Timeline or source evidence changed. Generate a fresh proposal.' if value['stale'] else None
    return value

def revision_list(store):
    state=store.load_state(); ctx=_context(store)
    suppressed=tl._base(state)['policies']['suppressedCategories']
    records=[r for r in _records(store) if r['status']!='pending' or r['plan']['kind'] not in suppressed]
    return {'suggestions':[_public(r,state['revision'],ctx['fingerprint']) for r in records],'revision':state['revision'],'warnings':[]}

def _generate(store,scope_end=None):
    state=store.load_state(); doc=tl._base(state); ctx=_context(store,True); suppressed=doc['policies']['suppressedCategories']
    mapping={'false_start':'false_start','silence':'long_silence','filler_words':'false_start','repeated_takes':'repeated_take','near_black':'visual_quality','blur':'visual_quality'}
    inferred=[]
    for transcript in ctx['data']['transcripts']:
        segments=sorted(transcript['segments'],key=lambda s:s['sourceInTicks'])
        for left,right in zip(segments,segments[1:]):
            a=_norm(left.get('text','')).split(); b=_norm(right.get('text','')).split()
            if len(a)>=2 and len(b)>=2 and a[:2]==b[:2] and left['sourceOutTicks']<=right['sourceInTicks'] and left['sourceOutTicks']-left['sourceInTicks']<=5_000_000:
                inferred.append({'assetId':transcript['assetId'],'sourceInTicks':left['sourceInTicks'],'sourceOutTicks':left['sourceOutTicks'],'detectorId':'false_start','detectorVersion':'1','result':{'explanation':'Weak repeated-prefix heuristic: adjacent timestamped transcript segments begin with the same two words. This may be an intentional repetition, not an abandoned start.','text':left['text']}})
    for row in ctx['analyses']+inferred:
        kind=mapping.get(row['detectorId'])
        if not kind or kind in suppressed: continue
        for clip in doc['clips']:
            if clip['itemType'] not in ('clip','audio') or clip['assetId']!=row['assetId']: continue
            a=max(row['sourceInTicks'],clip['sourceInTicks']); b=min(row['sourceOutTicks'],clip['sourceOutTicks'])
            start=clip['timelineStartTicks']+a-clip['sourceInTicks']; end=start+b-a
            if scope_end is not None: b=min(b,a+max(0,scope_end-start)); end=start+b-a
            if a>=b or (kind=='long_silence' and b-a<800_000): continue
            if scope_end is not None and kind!='long_silence': continue
            warnings=[]; evidence=[_evidence(row)]
            if kind=='long_silence':
                # Retain audible breathing room at each side; never cut a timed word.
                a+=150_000; b-=150_000
                words=[w for t in ctx['data']['transcripts'] if t['assetId']==clip['assetId'] for w in t['words']]
                if any(w['sourceInTicks']<b and w['sourceOutTicks']>a for w in words): continue
            if kind=='false_start': warnings=['Weak heuristic: filler words may be intentional speech, not a false start. Listen before accepting.']
            if kind=='repeated_take':
                match=row['result']; c,d=match.get('matchedSourceInTicks'),match.get('matchedSourceOutTicks')
                duration=next((a['duration_ticks'] for a in ctx['data'].get('assets',[]) if a['id']==row['assetId']),None)
                if type(c) is not int or type(d) is not int or type(duration) is not int or not 0<=c<d<=duration: continue
                evidence.append({'assetId':row['assetId'],'sourceInTicks':c,'sourceOutTicks':d,'detectorId':row['detectorId'],'detectorVersion':row['detectorVersion'],'explanation':'Alternative similar take; preference is not inferred.','text':match.get('matchedText','')})
                warnings=['Similarity does not establish which take is better. Review both takes.']
            start=clip['timelineStartTicks']+a-clip['sourceInTicks']; end=start+b-a
            commands=[{'type':'remove_range','clipId':clip['id'],'sourceInTicks':a,'sourceOutTicks':b,'ripple':kind in {'long_silence','false_start','repeated_take'}}]
            if kind=='visual_quality': warnings=['Removing this source range leaves a gap; speech may be affected. Review audio and image.']
            try: _save(store,doc,state,ctx,kind,{'long_silence':'Long silence','false_start':'Possible false start','repeated_take':'Possible repeated take','visual_quality':'Low-quality visual segment'}[kind],row['result'].get('explanation','Review this detected region.'),commands,evidence,start,end,warnings)
            except ValueError: continue
    if scope_end is None and ctx['brief']:
        brief=ctx['brief']; target=brief['constraints'].get('targetDurationMinutes')
        if type(target) in (float,int) and target>0 and _duration(doc)>target*60_000_000 and 'pacing' not in suppressed:
            _save(store,doc,state,ctx,'pacing','Timeline exceeds target duration','Manual review required: the timeline exceeds the saved duration constraint.',[],[],0,_duration(doc),['No automatic speech removal is proposed.'])
        trait=brief.get('traits',{}).get('averageShotLengthSeconds',{}).get('value')
        if trait is None: trait=brief['constraints'].get('styleTraits',{}).get('averageShotLengthSeconds')
        if type(trait) in (float,int) and 0<trait<=3600 and 'pacing' not in suppressed:
            for clip in doc['clips']:
                if clip['itemType']!='clip' or clip['timelineDurationTicks']<=trait*1_000_000: continue
                label=clip.get('properties',{}).get('sectionLabel','Selected passage')
                evidence=[{'assetId':clip['assetId'],'sourceInTicks':clip['sourceInTicks'],'sourceOutTicks':clip['sourceOutTicks'],'detectorId':'brief-pacing-comparison','detectorVersion':'1','explanation':f'This uninterrupted clip exceeds the saved {trait:g}s average shot length preference. An average is guidance, not a hard maximum.'}]
                _save(store,doc,state,ctx,'pacing','Section pacing needs review',f'{label}: this clip is {clip["timelineDurationTicks"]/1e6:g}s long. Review pacing against the saved average-shot preference.',[],evidence,clip['timelineStartTicks'],clip['timelineStartTicks']+clip['timelineDurationTicks'],['Manual review required. No speech is removed to enforce an average.'])
        text=' '.join(part.get('text','') for t in ctx['data']['transcripts'] for part in t['segments'] if any(c['assetId']==t['assetId'] and c['sourceInTicks']<part['sourceOutTicks'] and c['sourceOutTicks']>part['sourceInTicks'] for c in doc['clips']))
        for requirement in brief['constraints'].get('mustKeep',[]):
            if _norm(requirement) not in _norm(text) and 'unmet_requirement' not in suppressed:
                _save(store,doc,state,ctx,'unmet_requirement','Brief requirement needs review',f'Exact transcript evidence for “{requirement}” is absent from the timeline. Semantic coverage requires manual review.',[],[],0,_duration(doc),['No footage or speech is invented.'])
    return revision_list(store)

def revision_generate(store): return _generate(store)

def revision_propose(store,text,selected_suggestion_id=None,selected_clip_id=None):
    parsed=parse_revision(text)
    if parsed['intent'] in ('tighten','opening'):
        end=parsed.get('endTicks',30_000_000)
        if parsed['intent']=='opening':
            doc=tl.timeline_state(store)
            clips=sorted((c for c in doc['clips'] if c['itemType']=='clip'),key=lambda c:c['timelineStartTicks'])
            if clips and clips[0].get('properties',{}).get('sectionId'):
                section=clips[0]['properties']['sectionId']; plan=clips[0]['properties'].get('planId')
                end=max(c['timelineStartTicks']+c['timelineDurationTicks'] for c in clips if c.get('properties',{}).get('sectionId')==section and c.get('properties',{}).get('planId')==plan)
            else: end=min(end,doc['durationTicks'])
        response=_generate(store,end)
        response['suggestions']=[s for s in response['suggestions'] if s['kind']=='long_silence' and not s['stale'] and s['baseRevision']==response['revision'] and s['timelineInTicks']>=0 and s['timelineOutTicks']<=end and s['operations'] and all(c['type']=='remove_range' for c in s['operations'])]
        if not response['suggestions']: response['warnings'].append('No eligible measured pauses were found in this scope. No change was proposed.')
        response['warnings'].append(f'Interpreted as silence tightening from timeline 0 to {end/1e6:g}s. Speech and protected ranges are retained. No operation is applied until accepted.')
        return response
    state=store.load_state(); doc=tl._base(state); ctx=_context(store,True); intent=parsed['intent']; evidence=[]; commands=[]; restore=None; warnings=[]
    selected=next((r for r in _records(store) if r['id']==selected_suggestion_id),None)
    if intent=='protect':
        ranges=[]; phrase=parsed['phrase']; target=_norm(phrase)
        for t in ctx['data']['transcripts']:
            ranges.extend(_literal_ranges(t,phrase))
        unique={_json(r):r for r in ranges}; ranges=list(unique.values())
        if not ranges: raise ValueError('No exact transcript mentions were found. Review the transcript or enter an exact phrase.')
        commands=[{'type':'protect_ranges','ranges':ranges}]; title='Protect every exact mention'; explanation=f'Protect {len(ranges)} source range(s) matching “{phrase}”; source protection follows clips when moved.'; kind='protect_topic'
        evidence=[dict(r,detectorId='literal-transcript-match',detectorVersion='1',explanation='Exact reviewed or original transcript match.') for r in ranges]
    elif intent=='suppress':
        if not selected: raise ValueError('Select the suggestion category to dismiss everywhere. No footage will be removed.')
        kind=selected['plan']['kind']
        if kind not in tl.SUGGESTION_CATEGORIES: raise ValueError('Select a detected editing suggestion category to dismiss.')
        commands=[{'type':'suppress_category','category':kind}]; title='Dismiss this suggestion category'; explanation=f'Hide future {kind} suggestions in this project; footage remains intact.'
    elif intent=='punch_in':
        clips=[c for c in doc['clips'] if c.get('properties',{}).get('cropPreset') in ('punch_in_110','punch_in_125') and (selected_clip_id is None or c['id']==selected_clip_id)]
        if not clips: raise ValueError('No supported punch-ins exist in the selected scope.')
        commands=[{'type':'crop','clipId':c['id'],'cropPreset':'full_frame'} for c in clips]; kind='punch_in'; title='Use fewer punch-ins'; explanation='Restore source framing for the shown punch-ins.'
    else:
        if not selected or selected['status']!='accepted' or not any(c['type']=='remove_range' for c in selected['plan']['operations']): raise ValueError('Select an accepted removal to restore its longer explanation.')
        if tl._snapshot(doc)!=selected['plan']['after']: raise ValueError('Later edits changed this timeline. Undo later edits before restoring the explanation.')
        restore=deepcopy(doc); restore.update(deepcopy(selected['plan']['before'])); _validate_restoration(restore,ctx,doc); tl._validate(restore,state)
        kind='restore'; title='Restore longer explanation'; explanation='Restore the exact timeline state before the selected removal, including its source ranges and policies.'
    _save(store,doc,state,ctx,kind,title,explanation,commands,evidence,0,_duration(doc),warnings,restore)
    return revision_list(store)

def _validate_restoration(doc,ctx,current=None):
    excluded=[{'assetId':t['assetId'],**m} for t in ctx['data']['transcripts'] for m in t.get('markers',[]) if m['marker']=='excluded']
    brief=ctx['brief']; removes=brief['constraints'].get('mustRemove',[]) if brief else []
    for t in ctx['data']['transcripts']:
        for term in removes: excluded.extend(_literal_ranges(t,term))
    if any(_overlap(c,m) for c in doc['clips'] for m in excluded): raise ValueError('Restoration intersects an excluded source range; resolve its marker first.')
    if current is not None:
        previous={c['id']:c for c in current['clips']}
        if any(_locked(c,ctx) and previous.get(c['id'])!=c for c in doc['clips']): raise ValueError('Unlock the affected plan section before restoration.')
        restored={c['id']:c for c in doc['clips']}
        if any(_locked(c,ctx) and restored.get(c['id'])!=c for c in current['clips']): raise ValueError('Unlock the affected plan section before restoration.')
        for protected in _protected(current,ctx):
            for clip in current['clips']:
                if clip['itemType'] not in ('clip','audio') or not _overlap(clip,protected): continue
                start=max(clip['sourceInTicks'],protected['sourceInTicks']); end=min(clip['sourceOutTicks'],protected['sourceOutTicks'])
                spans=sorted((c['sourceInTicks'],c['sourceOutTicks']) for c in doc['clips'] if c['itemType'] in ('clip','audio') and c['assetId']==clip['assetId'])
                cursor=start
                for a,b in spans:
                    if a<=cursor: cursor=max(cursor,b)
                if cursor<end: raise ValueError('Restoration would remove required or protected source coverage.')

def revision_decide(store,suggestion_id,decision,expected_revision):
    if decision=='undone': decision='undo'
    if decision not in ('accepted','rejected','undo'): raise ValueError('Choose accepted, rejected, or undo.')
    if type(expected_revision) is not int: raise ValueError('Expected revision must be an integer.')
    with _connect(store.database_path) as db:
        db.execute('BEGIN IMMEDIATE'); state=store.load_state(db); doc=tl._base(state); ctx=_context(store,True)
        if state['revision']!=expected_revision: raise ProjectError('Timeline changed. Refresh before deciding.')
        row=db.execute('SELECT * FROM edit_plans WHERE id=? AND project_id=?',(suggestion_id,store.project_id)).fetchone()
        if row is None: raise ValueError('Suggestion was not found.')
        plan=json.loads(row['plan_json'])
        if plan.get('planType')!='timeline-revision': raise ValueError('Select a timeline revision suggestion.')
        status=row['status']; current=tl._snapshot(doc)
        if decision=='rejected':
            if status=='accepted': raise ValueError('Undo this accepted suggestion before rejecting it.')
            status='rejected'; result=tl._public(doc,state['revision'])
        else:
            if ctx['fingerprint']!=plan['evidenceFingerprint']: raise ValueError('Source evidence or constraints changed. Generate a fresh proposal.')
            if decision=='undo':
                if status!='accepted' or current!=plan['after'] or not doc['undo'] or doc['undo'][-1]['command']!={'type':'revision_accept','suggestionId':suggestion_id}: raise ValueError('Undo later edits first; this suggestion is no longer the latest edit.')
                entry=doc['undo'][-1]
                if entry['before']!=plan['before'] or entry['after']!=plan['after']: raise ValueError('The saved suggestion and reversible history disagree. Restore a verified checkpoint.')
                before=deepcopy(doc); before.update(deepcopy(plan['before'])); _validate_restoration(before,ctx,doc); tl._validate(before,state)
                doc['undo'].pop(); doc.update(deepcopy(plan['before'])); doc['redo'].append(entry); status='undone'
            else:
                if status!='pending' or state['revision']!=plan['baseRevision'] or current!=plan['before']: raise ValueError('Suggestion is stale or already decided. Generate it again.')
                if not plan['operations'] and not plan.get('restore'): raise ValueError('This suggestion requires manual review and has no automatic operation.')
                if plan.get('restore'):
                    after=deepcopy(doc); after.update(deepcopy(plan['after'])); _validate_restoration(after,ctx,doc); tl._validate(after,state)
                else: after=_simulate(doc,state,ctx,plan['operations'])
                # IDs created during preview differ; preserve the validated saved diff for review fidelity.
                if not plan.get('restore'):
                    saved=deepcopy(doc); saved.update(deepcopy(plan['after'])); tl._validate(saved,state)
                    if _canonical(tl._snapshot(after),current)!=_canonical(plan['after'],current): raise ValueError('The reviewed diff no longer matches its operations.')
                    after=saved
                doc.update(tl._snapshot(after)); doc['undo']=(doc['undo']+[{'command':{'type':'revision_accept','suggestionId':suggestion_id},'before':current,'after':tl._snapshot(doc)}])[-tl.LIMIT:]; doc['redo']=[]; status='accepted'
            result=tl._persist(store,db,state,doc)
        plan['status']=status; db.execute('UPDATE edit_plans SET status=?,plan_json=? WHERE id=?',(status,_json(plan),suggestion_id)); db.commit()
    if decision!='rejected': store._checkpoint_if_due(result['revision'])
    record={'id':suggestion_id,'status':status,'plan':plan}
    return {'suggestion':_public(record,result['revision'],ctx['fingerprint']),'timeline':result}


def _canonical(snapshot,before):
    value=deepcopy(snapshot)
    ids={}
    def collect(node):
        if isinstance(node,dict):
            if isinstance(node.get('id'),str): ids.setdefault(node['id'],'generated:'+str(len(ids)))
            for key in sorted(node): collect(node[key])
        elif isinstance(node,list):
            for item in node: collect(item)
    collect(before)
    ids={key:key for key in ids}
    collect(value)
    def replace(node):
        if isinstance(node,dict): return {k:replace(v) if k in ('id','linkedClipId','timelineItemId') or isinstance(v,(dict,list)) else v for k,v in node.items()}
        if isinstance(node,list): return [replace(v) for v in node]
        return ids.get(node,node) if isinstance(node,str) else node
    return replace(value)
