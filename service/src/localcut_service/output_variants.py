"""Atomic timeline variants share source asset IDs and source media references."""
from copy import deepcopy
import uuid
from .export_profiles import validate_profile
from .persistence import ProjectError, _connect
from .timeline import _base, _persist, _validate

def _collection(state):
    value=deepcopy(state['settings'].get('outputVariants',{'schemaVersion':1,'activeId':None,'variants':[]}))
    if not isinstance(value,dict) or set(value)!={'schemaVersion','activeId','variants'} or value['schemaVersion']!=1 or not isinstance(value['variants'],list): raise ValueError('Invalid output variant collection.')
    if len(value['variants'])>30: raise ValueError('Too many output variants.')
    ids=set()
    for v in value['variants']:
        if not isinstance(v,dict) or set(v)!={'id','name','profile','timeline','planIds'} or not isinstance(v['id'],str) or v['id'] in ids or not isinstance(v['name'],str) or not v['name'].strip() or len(v['name'])>120 or not isinstance(v['planIds'],list) or any(not isinstance(i,str) for i in v['planIds']): raise ValueError('Invalid output variant.')
        ids.add(v['id']); validate_profile(v['profile']); _validate(v['timeline'],state)
    if value['activeId'] is not None and value['activeId'] not in ids: raise ValueError('Active variant is missing.')
    return value

def list_variants(store):
    state=store.load_state(); values=_collection(state)
    return {'revision':state['revision'],'activeId':values['activeId'],'variants':[{k:deepcopy(v[k]) for k in ('id','name','profile','planIds')} for v in values['variants']]}

def _stash_active(values,doc,state):
    active=next((v for v in values['variants'] if v['id']==values['activeId']),None)
    if active:
        active['timeline']=deepcopy(doc)
        active['profile']=validate_profile(state['settings'].get('outputProfile',active['profile']))

def save_variant(store,name,profile,expected_revision,plan_ids=None):
    """Fork the current reviewed timeline; first save also preserves the base variant."""
    profile=validate_profile(profile)
    if not isinstance(name,str) or not name.strip() or len(name)>120: raise ValueError('Variant name must contain 1–120 characters.')
    if plan_ids is None: plan_ids=[]
    if not isinstance(plan_ids,list) or any(not isinstance(i,str) or not i for i in plan_ids): raise ValueError('Plan IDs must be nonempty strings.')
    if type(expected_revision) is not int: raise ValueError('Expected revision must be an integer.')
    with _connect(store.database_path) as connection:
        connection.execute('BEGIN IMMEDIATE'); state=store.load_state(connection)
        if state['revision']!=expected_revision: raise ProjectError('The timeline changed. Refresh before creating a variant.')
        values=_collection(state); doc=_base(state); _stash_active(values,doc,state)
        if len(values['variants'])>=30: raise ValueError('At most 30 output variants are supported.')
        if not values['variants']:
            base_profile=state['settings'].get('outputProfile',profile)
            values['variants'].append({'id':str(uuid.uuid4()),'name':'Original timeline','profile':validate_profile(base_profile),'timeline':deepcopy(doc),'planIds':[]})
        identifier=str(uuid.uuid4())
        values['variants'].append({'id':identifier,'name':name.strip(),'profile':profile,'timeline':deepcopy(doc),'planIds':deepcopy(plan_ids)})
        values['activeId']=identifier; state['settings']['outputVariants']=values; state['settings']['outputProfile']=profile
        result=_persist(store,connection,state,doc); connection.commit()
    store._checkpoint_if_due(result['revision'])
    return {**list_variants(store),'timeline':result}

def select_variant(store,variant_id,expected_revision):
    if not isinstance(variant_id,str) or type(expected_revision) is not int: raise ValueError('Invalid variant selection.')
    with _connect(store.database_path) as connection:
        connection.execute('BEGIN IMMEDIATE'); state=store.load_state(connection)
        if state['revision']!=expected_revision: raise ProjectError('The timeline changed. Refresh before switching variants.')
        values=_collection(state); _stash_active(values,_base(state),state)
        selected=next((v for v in values['variants'] if v['id']==variant_id),None)
        if selected is None: raise ValueError('Output variant does not exist.')
        values['activeId']=variant_id; state['settings']['outputVariants']=values; state['settings']['outputProfile']=deepcopy(selected['profile'])
        result=_persist(store,connection,state,deepcopy(selected['timeline'])); connection.commit()
    store._checkpoint_if_due(result['revision'])
    return {**list_variants(store),'timeline':result}
