import io
import time
import wave
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from sarae_agent.cloud.commands import TtsPayload
from sarae_agent.cloud.journal import EventOutbox
from sarae_agent.errors import AgentError
from sarae_agent.voice.omnivoice import OmniVoiceProvider
from sarae_agent.voice.tts import VoiceEngine
from sarae_agent.config import ConfigStore


def wav():
    output = io.BytesIO()
    with wave.open(output, 'wb') as w:
        w.setparams((1, 2, 24000, 0, 'NONE', ''))
        w.writeframes(b'\0\0' * 2400)
    return output.getvalue()


def ref(seconds=60):
    return dict(utterance_id='a'*32, job_id='job_test', voice_rev=1, audio_path='auto',
                deadline_at=(datetime.now(timezone.utc)+timedelta(seconds=seconds)).isoformat())


def provider(handler):
    return OmniVoiceProvider(lambda: (SimpleNamespace(agent_id='agent', credential='secret'), 'http://backend/api'),
                             httpx.MockTransport(handler))


@pytest.mark.parametrize('direct', [True, False])
def test_download_paths(tmp_path, direct):
    calls=[]
    def handle(r):
        calls.append(r)
        if r.method == 'POST':
            if direct:
                return httpx.Response(200, json=dict(job_id='job_test', base_url='http://gpu', read_token='read'))
            return httpx.Response(404, headers={'X-Sarae-Error':'USE_RELAY'})
        return httpx.Response(200, content=wav())
    out=tmp_path/'audio.wav'
    info=provider(handle).download(ref(),out,lambda: None,'auto')
    assert info['audio_path'] == ('direct' if direct else 'relay')
    assert out.read_bytes()==wav()
    assert all(r.method in ('GET','POST') for r in calls)
    assert all('/jobs' not in str(r.url) or r.method=='GET' for r in calls)
    if direct:
        assert 'X-Sarae-Agent-Key' not in calls[-1].headers


@pytest.mark.parametrize('status,code', [(401,'TTS_AUTH'),(403,'TTS_AUTH'),(409,'TTS_INFERENCE_FAILED'),
                                        (410,'TTS_EXPIRED'),(429,'TTS_BUSY'),(503,'TTS_NOT_READY')])
def test_http_errors_do_not_relay(tmp_path,status,code):
    calls=[]
    def handle(r):
        calls.append(r)
        if r.method=='POST':
            return httpx.Response(200,json=dict(job_id='job_test',base_url='http://gpu',read_token='r'))
        return httpx.Response(status)
    with pytest.raises(AgentError) as e:
        provider(handle).download(ref(),tmp_path/'x.wav',lambda:None,'auto')
    assert e.value.code==code
    assert len(calls)==2


def test_direct_network_failure_relays_and_polls(tmp_path):
    count=0
    def handle(r):
        nonlocal count
        if r.method=='POST':
            return httpx.Response(200,json=dict(job_id='job_test',base_url='http://gpu',read_token='r'))
        if r.url.host=='gpu':
            raise httpx.ConnectError('offline')
        count+=1
        return httpx.Response(202) if count==1 else httpx.Response(200,content=wav())
    assert provider(handle).download(ref(),tmp_path/'x.wav',lambda:None,'auto')['audio_path']=='relay'
    assert count==2


def test_deadline_after_download(tmp_path):
    expired=False
    def handle(r):
        nonlocal expired
        expired=True
        return httpx.Response(200,content=wav())
    def check():
        if expired:
            raise AgentError('TTS_EXPIRED','expired')
    with pytest.raises(AgentError):
        provider(handle).download(ref(),tmp_path/'x.wav',check)
    assert not (tmp_path/'x.wav').exists()


class Obs:
    def __init__(self):
        self.played=[]
        self.poll=0
    def play_file(self,source,path,**kw):
        self.played.append(path)
        self.poll=0
        return {}
    def media_status(self,source):
        self.poll+=1
        return {'state':'OBS_MEDIA_STATE_PLAYING' if self.poll==1 else 'OBS_MEDIA_STATE_ENDED'}
    def media_action(self,*args):
        pass


def idle(v):
    end=time.monotonic()+3
    while time.monotonic()<end:
        if not v.status()['queue'] and not v.status()['speaking']:
            return
        time.sleep(.01)
    raise AssertionError(v.status())


def test_playback_dedup_restart_intro_and_events(home):
    obs=Obs()
    v=VoiceEngine(ConfigStore(),obs)
    v.POLL_SECONDS=.001
    events=[]
    v.event_sink=events.append
    v.job_provider=provider(lambda r:httpx.Response(200,content=wav()))
    tts=ref();tts['audio_path']='relay';tts['intro']=dict(utterance_id='b'*32,job_id='intro_job')
    options=dict(provider='omnivoice',voice='c'*32,tts=tts,duck=False,intro='Lan ơi')
    assert v.speak('Xin chào',**options)['status']=='queued'
    idle(v)
    assert len(obs.played)==2
    assert all(p.endswith('.wav') for p in obs.played)
    assert events[-1]['detail']['status']=='played'
    v.close()
    v2=VoiceEngine(ConfigStore(),obs)
    assert v2.speak('Xin chào',**options)['status']=='duplicate'
    v2.close()
    assert len(obs.played)==2


def test_expired_command_is_persistently_dropped(home):
    v=VoiceEngine(ConfigStore(),Obs())
    options=dict(provider='omnivoice',voice='c'*32,tts=ref(-3),duck=False)
    assert v.speak('Xin chào',**options)['status']=='expired'
    assert v.speak('Xin chào',**options)['status']=='duplicate'
    v.close()


def test_outbox_restart_resends_same_sequence(tmp_path):
    p=tmp_path/'events.json'
    box=EventOutbox(path=p)
    box.push({'type':'tts_playback','detail':{}})
    batch=[]
    restored=EventOutbox(path=p)
    restored.flush(batch.extend)
    assert len(batch)==1
    assert EventOutbox(path=p).pending()==0


def test_schema_accepts_phase3_contract():
    data=TtsPayload.model_validate(dict(text='Xin chào',voice='c'*32,provider='omnivoice',
                                        model='demo_default',tts=ref()))
    assert data.tts.job_id=='job_test'


def test_prepare_then_play_uses_job_cache(home):
    calls=[]
    def handle(r):
        calls.append(r)
        return httpx.Response(200,content=wav())
    v=VoiceEngine(ConfigStore(),Obs());v.POLL_SECONDS=.001
    v.job_provider=provider(handle)
    tts=ref();tts['audio_path']='relay'
    v.prepare(['Xin chào'],voice='c'*32,provider='omnivoice',tts_jobs=[{k:tts[k] for k in ('utterance_id','job_id')}])
    end=time.monotonic()+3
    while time.monotonic()<end and not list(v._cache_dir.glob('*.wav')):
        time.sleep(.01)
    assert list(v._cache_dir.glob('*.wav'))
    v.speak('Xin chào',voice='c'*32,provider='omnivoice',tts=tts,duck=False)
    idle(v)
    assert len(calls)==1
    v.close()


@pytest.mark.parametrize('code,allow', [('TTS_EXPIRED',False),('TTS_CANCELLED',False),
                                       ('TTS_AUTH',False),('TTS_BUSY',True),('TTS_INFERENCE_FAILED',True)])
def test_fallback_only_allowed_errors(home,code,allow):
    v=VoiceEngine(ConfigStore(),Obs());v.POLL_SECONDS=.001
    class Broken:
        def download(self,*a):
            raise AgentError(code,'failure')
    v.job_provider=Broken()
    called=[]
    def audio(*args):
        called.append(args)
        path=v._cache_dir/'fallback.mp3';path.write_bytes(b'mp3');return path
    v._audio=audio
    policy={'mode':'provider','provider':{'provider':'edge','voice':'vi-VN-HoaiMyNeural'}}
    v.speak('Xin chào',voice='c'*32,provider='omnivoice',tts=ref(),duck=False,fallback=policy)
    idle(v)
    assert bool(called)==allow
    v.close()


def test_clear_during_download_prevents_playback(home):
    import threading
    entered=threading.Event();release=threading.Event()
    class Slow:
        def download(self,ref,out,check,*args):
            entered.set();release.wait(2);check()
            out.write_bytes(wav());return {}
    obs=Obs();v=VoiceEngine(ConfigStore(),obs);v.job_provider=Slow()
    v.speak('Xin chào',voice='c'*32,provider='omnivoice',tts=ref(),duck=False)
    assert entered.wait(2)
    v.clear();release.set();idle(v)
    assert not obs.played
    assert v.last['status']=='cancelled'
    v.close()


def test_process_crash_after_acceptance_does_not_replay(home):
    import subprocess
    import sys
    from pathlib import Path
    source = str(Path(__file__).resolve().parents[1] / 'src')
    code = f'''
import sys, os
sys.path.insert(0, {source!r})
from sarae_agent.voice.tts import VoiceEngine
from sarae_agent.config import ConfigStore
v = VoiceEngine(ConfigStore(), None)
v._ensure_worker = lambda: None
r = v.speak('Xin chào', provider='omnivoice', voice='{'c'*32}', tts={ref()!r}, duck=False)
assert r['status'] == sys.argv[1], r
os._exit(0)
'''
    subprocess.run([sys.executable, '-c', code, 'queued'], check=True)
    subprocess.run([sys.executable, '-c', code, 'duplicate'], check=True)


def test_outbox_overflow_does_not_erase_acceptance(home):
    from sarae_agent.config import paths
    box=EventOutbox(limit=2,path=paths.root/'outbox.json')
    voice=VoiceEngine(ConfigStore(),None);voice._ensure_worker=lambda:None
    voice.event_sink=box.push
    options=dict(provider='omnivoice',voice='c'*32,tts=ref(),duck=False)
    assert voice.speak('Xin chào',**options)['status']=='queued'
    for i in range(5):box.push({'type':'tts_playback','detail':{'status':'sample','i':i}})
    assert box.pending()==2
    voice.close()
    restarted=VoiceEngine(ConfigStore(),None)
    assert restarted.speak('Xin chào',**options)['status']=='duplicate'
    restarted.close()


def test_relay_unreachable_is_bounded(tmp_path):
    calls=[]
    def offline(r):
        calls.append(r)
        raise httpx.ConnectError('offline')
    with pytest.raises(AgentError) as error:
        provider(offline).download(ref(),tmp_path/'x.wav',lambda:None,'auto')
    assert error.value.code=='TTS_BACKEND_UNREACHABLE'
    assert len(calls)==2


def test_cancelled_job_does_not_relay(tmp_path):
    calls=[]
    def cancel(r):
        calls.append(r)
        return httpx.Response(410,json={'error':{'code':'JOB_CANCELLED'}})
    with pytest.raises(AgentError) as error:
        provider(cancel).download(ref(),tmp_path/'x.wav',lambda:None,'relay')
    assert error.value.code=='TTS_CANCELLED'
    assert len(calls)==1


def test_obs_unavailable_never_falls_back(home):
    class MissingObs(Obs):
        def play_file(self,*args,**kwargs):raise AgentError('OBS_NOT_CONNECTED','offline')
    v=VoiceEngine(ConfigStore(),MissingObs());v.POLL_SECONDS=.001
    v.job_provider=provider(lambda r:httpx.Response(200,content=wav()))
    events=[];v.event_sink=events.append
    v.speak('Xin chào',voice='c'*32,provider='omnivoice',tts={**ref(),'audio_path':'relay'},duck=False,
            fallback={'mode':'provider','provider':{'provider':'edge','voice':'vi-VN-HoaiMyNeural'}})
    idle(v)
    assert events[-1]['detail']['status']=='failed'
    assert events[-1]['detail']['error_code']=='OBS_NOT_CONNECTED'
    v.close()
