"""Closed, numeric finishing settings; no caller supplied filters or commands."""
import math


def render_options(value=None):
    value = {} if value is None else value
    defaults = dict(mode="final", resolution=1080, frameRate=30, speechGainDb=0,
                    normalize=False, loudnessLufs=-16, noiseReduction=False, captionStyle="plain", safeArea=0.08, outputProfile=None)
    if not isinstance(value, dict) or set(value) - set(defaults):
        raise ValueError("Unknown render settings.")
    result = {**defaults, **value}
    if result['outputProfile'] is not None:
        from .export_profiles import validate_profile
        result['outputProfile']=validate_profile(result['outputProfile'])
    if result['mode'] not in ('preview', 'final') or result['resolution'] not in (480, 720, 1080) or type(result['resolution']) is not int:
        raise ValueError('Choose preview/final and a 480, 720, or 1080 resolution.')
    if type(result['frameRate']) is not int or result['frameRate'] not in (24, 25, 30, 50, 60):
        raise ValueError('Choose 24, 25, 30, 50, or 60 fps.')
    for key, low, high in (('speechGainDb', -60, 24), ('loudnessLufs', -24, -9), ('safeArea', .05, .2)):
        v = result[key]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not low <= v <= high:
            raise ValueError(f'{key} must be between {low} and {high}.')
    if any(type(result[k]) is not bool for k in ('normalize', 'noiseReduction')) or result['captionStyle'] not in ('plain', 'boxed', 'large'):
        raise ValueError('Invalid audio or caption preset.')
    if result['mode'] == 'preview':
        result['resolution'] = min(720, result['resolution'])
    return result


def finish_audio(label, options):
    filters = []
    if options['normalize']:
        filters.append(f"loudnorm=I={options['loudnessLufs']}:TP=-1.5:LRA=11")
    # Always protect peaks, including timelines with no music bed. Disable automatic
    # makeup gain and compensate the limiter's lookahead latency.
    filters.append('alimiter=limit=0.891251:level=0:latency=1')
    return label + ','.join(filters) + '[afinished]'
