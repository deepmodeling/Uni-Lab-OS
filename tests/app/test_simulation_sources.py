from copy import deepcopy

import pytest

from unilabos.simulation.sources import SourceBarrier, SourcePort


def frame():
    return {'contact':dict(source='isaac',unit='1',value=True,quality='good',captured_tick=1,sequence=1,
                           world_id='w',session_id='s',epoch=0)}


@pytest.mark.parametrize('key,value',[('source','analytic'),('unit','mL'),('epoch',1),('session_id','old'),('quality','stale'),('value',1),('captured_tick',2)])
def test_invalid_source_cannot_be_consumed(key,value):
    barrier=SourceBarrier({'contact':SourcePort('isaac','1',bool)},dict(world_id='w',session_id='s',epoch=0))
    bad=frame();bad['contact'][key]=value
    with pytest.raises(ValueError):barrier.consume(bad,tick=1)
    assert barrier.last=={}


def test_duplicate_does_not_refresh_ttl_or_capture_time():
    barrier=SourceBarrier({'contact':SourcePort('isaac','1',bool)},dict(world_id='w',session_id='s',epoch=0))
    f=frame();assert barrier.consume(f,tick=1)=={'contact':True}
    assert barrier.consume(f,tick=2)=={'contact':True}
    with pytest.raises(ValueError):barrier.consume(f,tick=4)
    bad=deepcopy(f);bad['contact']['captured_tick']=2
    with pytest.raises(ValueError,match='relabelled'):barrier.consume(bad,tick=2)
