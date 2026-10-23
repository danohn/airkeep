from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization
import httpx
import jwt
from config import Settings
from push import APNs


async def test_environment_key_separation_and_activity_headers(tmp_path):
    paths=[]
    for name in ['sandbox','production']:
        key=ec.generate_private_key(ec.SECP256R1())
        path=tmp_path/(name+'.p8')
        path.write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
        paths.append(path)
    settings=Settings(api_token='x'*40,apns_team_id='TESTTEAM',apns_key_id='DEVKEY',apns_key_path=str(paths[0]),
        apns_key_environment='sandbox',apns_production_key_id='PRODKEY',apns_production_key_path=str(paths[1]),_env_file=None)
    requests=[]
    def receive(request):
        requests.append(request)
        return httpx.Response(200)
    push=APNs(settings)
    await push.client.aclose()
    push.client=httpx.AsyncClient(transport=httpx.MockTransport(receive))
    assert await push.send('a'*64,'com.example.app','sandbox',{'aps':{'alert':'test'}})==200
    assert await push.send('b'*64,'com.example.app','production',{'aps':{'event':'update'}},live=True)==200
    assert requests[0].url.host=='api.sandbox.push.apple.com'
    assert requests[1].url.host=='api.push.apple.com'
    assert requests[1].headers['apns-topic']=='com.example.app.push-type.liveactivity'
    assert requests[1].headers['apns-push-type']=='liveactivity'
    assert requests[1].headers['apns-priority']=='5'
    assert jwt.get_unverified_header(requests[0].headers['authorization'].split()[1])['kid']=='DEVKEY'
    assert jwt.get_unverified_header(requests[1].headers['authorization'].split()[1])['kid']=='PRODKEY'
    await push.close()


def test_sandbox_only_key_is_not_used_for_production():
    settings=Settings(api_token='x'*40,apns_team_id='T',apns_key_id='D',apns_key_path='/fake',apns_key_environment='sandbox',_env_file=None)
    assert settings.apns_ready
    assert settings.apns_credentials('sandbox')==('D','/fake')
    assert settings.apns_credentials('production') is None
