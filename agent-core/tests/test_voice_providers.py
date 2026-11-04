"""Supported provider contracts and rejection of retired commands."""
import pytest
from pydantic import ValidationError
from sarae_agent.cloud.commands import TtsPayload, TtsPreparePayload
from sarae_agent.ipc.methods import SpeakParams
from sarae_agent.errors import AgentError
from sarae_agent.voice.providers import EdgeTTSProvider, ProviderRegistry, default_registry
from sarae_agent.voice.tts import VoiceService


def test_removed_provider_is_not_registered_or_accepted_by_command_schemas():
    with pytest.raises(AgentError) as error:
        default_registry().get('elevenlabs')
    assert error.value.code == 'TTS_PROVIDER'
    for schema, data in ((TtsPayload, {'text':'Xin chào'}),
                         (TtsPreparePayload, {'texts':['Xin chào']}),
                         (SpeakParams, {'text':'Xin chào'})):
        with pytest.raises(ValidationError):
            schema.model_validate({**data, 'provider':'elevenlabs'})


def test_edge_normalization_and_invalid_settings():
    provider = default_registry().get('edge')
    assert isinstance(provider, EdgeTTSProvider)
    assert provider.normalize(None, 'vi-VN-NamMinhNeural', 10, {}) == ('edge-neural', {'rate':10})
    for model, voice, rate, settings in [('old-model','vi-VN-HoaiMyNeural',0,{}),
                                       (None,'old-voice',0,{}), (None,'vi-VN-HoaiMyNeural',51,{}),
                                       (None,'vi-VN-HoaiMyNeural',0,{'speed':1.1})]:
        with pytest.raises(AgentError):
            provider.normalize(model, voice, rate, settings)


def test_cache_key_isolates_provider_model_voice_and_settings():
    base = VoiceService._key('Xin chào','vi-VN-HoaiMyNeural',0,'edge','edge-neural',{})
    assert base != VoiceService._key('Xin chào','vi-VN-NamMinhNeural',0,'edge','edge-neural',{})
    assert base != VoiceService._key('Xin chào','vi-VN-HoaiMyNeural',10,'edge','edge-neural',{})
    assert base != VoiceService._key('Xin chào','vi-VN-HoaiMyNeural',0,'other','edge-neural',{})
    assert base != VoiceService._key('Xin chào','vi-VN-HoaiMyNeural',0,'edge','other',{})
    assert base != VoiceService._key('Xin chào','vi-VN-HoaiMyNeural',0,'edge','edge-neural',{'rate':10})


def test_registry_does_not_fallback():
    with pytest.raises(AgentError) as error:
        ProviderRegistry().get('missing')
    assert error.value.code == 'TTS_PROVIDER'
