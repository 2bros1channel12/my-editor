import math

import pytest

from localcut_service.reframing import Detection, build_reframe_keyframes


def face(x, confidence=.9):
    return Detection(x,.2,.12,.2,confidence)


def build(samples, **kwargs):
    return build_reframe_keyframes(samples,1920,1080,1080,1920,**kwargs)


def test_horizontal_to_vertical_crop_preserves_aspect_and_source_time():
    frame=build([(2,[face(.7)])])["keyframes"][0]
    assert frame["sourceTicks"] == 2_000_000
    assert frame["width"]*1920/(frame["height"]*1080) == pytest.approx(1080/1920)
    assert frame["x"] <= .76 <= frame["x"]+frame["width"]


def test_vertical_to_horizontal_never_stretches():
    frame=build_reframe_keyframes([(0,[face(.3)])],1080,1920,1920,1080)["keyframes"][0]
    assert frame["width"]*1080/(frame["height"]*1920) == pytest.approx(1920/1080)
    assert 0 <= frame["y"] <= 1-frame["height"]


def test_multiple_faces_selection_has_stable_spatial_ids():
    samples=[(0,[face(.1),face(.7)]),(1,[face(.12),face(.68)])]
    result=build(samples,selected_subject_id="subject-2")
    assert len(result["subjects"]) == 2
    assert result["keyframes"][0]["subjectId"] == "subject-2"
    assert result["keyframes"][0]["x"] > .5


def test_wide_group_fits_both_people():
    frame=build([(0,[face(.05),face(.8)])],keep_both=True)["keyframes"][0]
    assert frame["layoutMode"] == "fit"
    assert frame["x"] == frame["y"] == 0
    assert frame["width"] == frame["height"] == 1


def test_moving_subject_is_smoothed_velocity_bounded_and_inside_frame():
    frames=build([(i*.2,[face(.1+i*.04)]) for i in range(15)])["keyframes"]
    assert frames[-1]["x"] > frames[0]["x"]
    for before,after in zip(frames,frames[1:]):
        assert abs(after["x"]-before["x"]) <= .18*.2+1e-10
        assert 0 <= after["x"] <= 1-after["width"]+1e-10


def test_low_confidence_defaults_center():
    frame=build([(0,[face(.8,.2)])])["keyframes"][0]
    assert frame["x"]+frame["width"]/2 == pytest.approx(.5)
    assert frame["confidence"] == 0
    assert "Low confidence" in frame["explanation"]


def test_variants_share_detection_source_and_are_independent():
    samples=[(0,[face(.5)])]
    vertical=build(samples)
    horizontal=build_reframe_keyframes(samples,1920,1080,1920,1080)
    assert vertical["keyframes"][0]["width"] < 1
    assert horizontal["keyframes"][0]["width"] == 1
    assert samples[0][1][0] == face(.5)


def test_invalid_source_coordinates_or_times_rejected():
    with pytest.raises(ValueError): build([(0,[face(.95)])])
    with pytest.raises(ValueError): build([(math.nan,[])])
    with pytest.raises(ValueError): build([(1,[]),(0,[])])


def test_tracking_cancellation_propagates():
    def cancel(): raise InterruptedError("cancel")
    with pytest.raises(InterruptedError): build([(0,[])],check_cancelled=cancel)


def test_salient_nonhuman_region_and_blank_background_fallback():
    np=pytest.importorskip('numpy');pytest.importorskip('cv2')
    from localcut_service.reframing import salient_region
    image=np.full((180,320,3),110,dtype=np.uint8)
    assert salient_region(image)==[]
    image[45:120,220:280]=[0,0,240]
    regions=salient_region(image)
    assert len(regions)==1 and regions[0].kind=='salient-region'
    assert regions[0].x>.6 and .55<=regions[0].confidence<=.68
    frame=build([(0,regions)])["keyframes"][0]
    assert frame['x']>.5


def test_equal_visual_regions_abstain_instead_of_arbitrary_tracking():
    np=pytest.importorskip('numpy');pytest.importorskip('cv2')
    from localcut_service.reframing import salient_region
    image=np.full((180,320,3),110,dtype=np.uint8)
    image[45:120,35:95]=[0,0,240];image[45:120,220:280]=[0,0,240]
    assert salient_region(image)==[]


def test_asymmetric_safe_area_keeps_selected_face_in_usable_crop():
    safe=dict(left=.05,right=.4,top=.08,bottom=.2)
    subject=Detection(.6,.2,.12,.2,.9)
    frame=build([(0,[subject])],safe_area=safe)['keyframes'][0]
    assert frame['layoutMode']=='crop'
    assert subject.x>=frame['x']+safe['left']*frame['width']-1e-9
    assert subject.x+subject.width<=frame['x']+(1-safe['right'])*frame['width']+1e-9


def test_impossible_safe_area_group_uses_explained_fit():
    frame=build([(0,[face(.01),face(.86)])],keep_both=True,safe_area=dict(left=.1,right=.1,top=.1,bottom=.1))['keyframes'][0]
    assert frame['layoutMode']=='fit' and 'safe area' in frame['explanation']
