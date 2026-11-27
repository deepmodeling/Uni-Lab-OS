"""孪生跟随公共层：协议无关的日志事件 → 行为表开始运动，成功应答后提交状态，失败不提交。"""
from unilabos.simulation.twin import CommandEvent, TwinFollower


class Journal:
    def __init__(self):
        self.rows = {}

    def put(self, **row):
        self.rows[row["command_id"]] = CommandEvent(**{"device_id": "31", "action": "rotate", "params": {}, **row})

    def events(self):
        return list(self.rows.values())


class Behaviours:
    def __init__(self):
        self.begun, self.state = [], {}

    def begin(self, event):
        self.begun.append(event.command_id)
        return lambda: self.state.__setitem__(event.command_id, "committed")

    def commit(self, after):
        after()


def test_twin_starts_on_send_and_commits_only_successful_results():
    journal, behaviours = Journal(), Behaviours()
    follower = TwinFollower(journal, behaviours)
    journal.put(command_id="a", sent_at=None)
    assert follower.step() == 0 and behaviours.begun == []           # 未发出不演
    journal.put(command_id="a", sent_at="t1")
    journal.put(command_id="b", sent_at="t1")
    assert follower.step() == 2 and behaviours.begun == ["a", "b"]
    journal.put(command_id="a", sent_at="t1", finished_at="t2", ok=True)
    journal.put(command_id="b", sent_at="t1", finished_at="t2", ok=False)
    follower.step()
    assert behaviours.state == {"a": "committed"}                     # 失败的不提交
    assert follower.step() == 0                                         # 终态只处理一次
    assert [h["kind"] for h in follower.history] == ["started", "started", "finished", "finished"]
