import time
from domain import AlertRule, AlertState, Reading, parse_readings, activity_payload

def reading(humidity=35, at=0):
    return Reading(amsID="0",humidity=humidity,temperature=31.6,rawIndex=3,observedAt=at)

def test_real_report_and_delta_and_invalid_sensor():
    p={"print":{"ams":{"ams":[{"id":"0","humidity_raw":"24","humidity":"3","temp":"31.6"}]}}}
    assert parse_readings(p,123)[0] == reading(24,123)
    for value in [None,"0","NaN","-1","101","oops"]:
        p["print"]["ams"]["ams"][0]["humidity_raw"]=value
        assert parse_readings(p)==[]
    assert parse_readings({"print":{"ams":{"ams":[{"id":"0","humidity":"3"}]}}})==[]
    assert parse_readings({"print":None})==[]

def test_continuous_hold_hysteresis_and_recovery_once():
    rule=AlertRule(enabled=True,threshold=30,holdSeconds=300,hysteresis=3)
    s=AlertState()
    assert s.evaluate(rule,reading(at=0),0,90) is None
    assert s.evaluate(rule,reading(at=299),299,90) is None
    assert s.evaluate(rule,reading(at=300),300,90)=="high"
    assert s.evaluate(rule,reading(29,301),301,90) is None
    assert s.evaluate(rule,reading(27,302),302,90)=="recovered"
    assert s.evaluate(rule,reading(26,303),303,90) is None

def test_stale_data_breaks_hold_and_does_not_recover_active_alert():
    rule=AlertRule(enabled=True,holdSeconds=300)
    s=AlertState()
    s.evaluate(rule,reading(at=0),0,90)
    s.evaluate(rule,reading(at=0),100,90)
    assert s.since is None
    assert s.evaluate(rule,reading(at=301),301,90) is None
    s.active=True
    assert s.evaluate(rule,reading(10,0),400,90) is None
    assert s.active

def test_live_payload_matches_swift_numeric_epoch_contract():
    p=activity_payload(reading(24,1000),1050,90)
    assert p["aps"]["content-state"]=={"humidity":24,"temperature":31.6,"observedAt":1000,"connected":True}
    assert p["aps"]["stale-date"]==1410
    assert p["aps"]["relevance-score"]==50
    assert activity_payload(reading(24,1000),1200,90)["aps"]["content-state"]["connected"] is False
    assert activity_payload(None,1200,90,end=True)["aps"]["dismissal-date"]==1200

def test_live_freshness_covers_push_interval_but_preserves_sensor_age():
    first = activity_payload(reading(24,1000),1050,90)["aps"]
    second = activity_payload(reading(24,1300),1350,90)["aps"]
    assert first["stale-date"] > second["timestamp"]
    assert first["content-state"]["observedAt"] == 1000
    assert second["content-state"]["observedAt"] == 1300
    assert second["stale-date"] == 1710
    for value in (reading(24,1000), None):
        old = activity_payload(value,1350,90)["aps"]
        assert old["content-state"]["connected"] is False
        assert old["stale-date"] == 1350
    assert activity_payload(reading(24,1000),1050,90,expires_at=1080)["aps"]["stale-date"] == 1080
