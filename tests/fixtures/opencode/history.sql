CREATE TABLE session (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, slug TEXT NOT NULL,
  directory TEXT NOT NULL, title TEXT NOT NULL, version TEXT NOT NULL,
  cost REAL NOT NULL DEFAULT 0, time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL);
CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, time_created INTEGER NOT NULL,
  time_updated INTEGER NOT NULL, data TEXT NOT NULL);
CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT NOT NULL, session_id TEXT NOT NULL,
  time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL, data TEXT NOT NULL);
INSERT INTO session VALUES ('ses_1','prj_1','brave-otter','/home/alice/src/app','Fix login','1.14.2',0,1790960000000,1791028000000);
-- Zen: two step-finish parts (0.0421 + 0.0420); message-level cost must not be double counted
INSERT INTO message VALUES ('msg_zen1','ses_1',1791022500000,1791022601000,'{"role":"assistant","time":{"created":1791022500000,"completed":1791022601000},"modelID":"claude-sonnet-4-6","providerID":"opencode","cost":0.0841,"tokens":{"input":201,"output":388,"reasoning":64,"cache":{"read":20600,"write":0}},"finish":"stop"}');
INSERT INTO part VALUES ('prt_zen1a','msg_zen1','ses_1',1791022542000,1791022542000,'{"type":"step-finish","reason":"tool-calls","cost":0.0421,"tokens":{"input":156,"output":590,"reasoning":0,"cache":{"read":20198,"write":0}}}');
INSERT INTO part VALUES ('prt_zen1b','msg_zen1','ses_1',1791022590000,1791022590000,'{"type":"step-finish","reason":"stop","cost":0.0420,"tokens":{"input":201,"output":388,"reasoning":64,"cache":{"read":20600,"write":0}}}');
INSERT INTO message VALUES ('msg_zen2','ses_1',1790883600000,1790883700000,'{"role":"assistant","time":{"created":1790883600000},"modelID":"gpt-5.6","providerID":"opencode","cost":1.25}');
-- Ignored: user message, too-old message, broken JSON
INSERT INTO message VALUES ('msg_u1','ses_1',1791022400000,1791022400000,'{"role":"user","time":{"created":1791022400000},"model":{"providerID":"opencode","modelID":"claude-sonnet-4-6"}}');
INSERT INTO message VALUES ('msg_old','ses_1',1787000000000,1787000000000,'{"role":"assistant","time":{"created":1787000000000},"modelID":"gpt-5.6","providerID":"opencode","cost":9.99}');
INSERT INTO message VALUES ('msg_bad','ses_1',1791020000000,1791020000000,'not json');
-- OpenCode Go rows
INSERT INTO message VALUES ('msg_A','ses_1',1791022500000,1791022601000,'{"role":"assistant","time":{"created":1791022500000},"modelID":"kimi-k2.7-code","providerID":"opencode-go","cost":0.421}');
INSERT INTO part VALUES ('prt_A1','msg_A','ses_1',1791022542000,1791022542000,'{"type":"step-finish","reason":"tool-calls","cost":0.183}');
INSERT INTO part VALUES ('prt_A2','msg_A','ses_1',1791022590000,1791022590000,'{"type":"step-finish","reason":"stop","cost":0.238}');
INSERT INTO message VALUES ('msg_B','ses_1',1791014700000,1791014800000,'{"role":"assistant","time":{"created":1791014700000},"modelID":"glm-5.3","providerID":"opencode-go","cost":1.9}');
INSERT INTO message VALUES ('msg_C','ses_1',1790883600000,1790883700000,'{"role":"assistant","time":{"created":1790883600000},"modelID":"minimax-m3","providerID":"opencode-go","cost":3.15}');
INSERT INTO message VALUES ('msg_D','ses_1',1790665200000,1790665300000,'{"role":"assistant","time":{"created":1790665200000},"modelID":"qwen3.7-plus","providerID":"opencode-go","cost":6.02}');
INSERT INTO message VALUES ('msg_E','ses_1',1790002800000,1790002900000,'{"role":"assistant","time":{"created":1790002800000},"modelID":"kimi-k2.6","providerID":"opencode-go","cost":4.459}');
