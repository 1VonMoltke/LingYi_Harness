"""Report confirmed unit losses from the LingYi side event stream."""

from engine.LingYi.entitys.gameenvs.utils import UNIT_DESTROYED_TYPE

TRACKED_CATEGORIES = {"Aircraft", "Facility", "Ship", "Submarine", "Vehicle", "Satellite"}


class UnitLossMonitor:
    def __init__(self, record):
        self.record = record
        self.side_names = {}
        self.loss_status = {}
        self.snapshots = {}

    def register_side(self, agent):
        side_id = getattr(agent, "ID", None)
        if side_id:
            self.side_names[str(side_id)] = agent.Name

    def register_situation(self, agent, situation):
        self.snapshots[agent.Name] = self._units(situation)

    @staticmethod
    def _units(situation):
        if not situation or not situation[0]:
            return {}
        return {guid: unit for guid, unit in situation[0].items()
                if unit.get("category") in TRACKED_CATEGORIES}

    @staticmethod
    def _round(agent):
        return getattr(agent, "attempts", 0) or None

    def observe(self, agent, simulation_seconds, situation=None):
        self.register_side(agent)
        messages = getattr(agent, "KeyMsg", None) or {}
        losses = messages.get(UNIT_DESTROYED_TYPE, messages.get(str(UNIT_DESTROYED_TYPE), []))
        for loss in losses:
            if not isinstance(loss, dict) or loss.get("IsDamaged") is True:
                continue
            unit_id = loss.get("UnitID")
            if not unit_id or self.loss_status.get(unit_id) == "confirmed":
                continue
            unit_class = str(loss.get("UnitClass") or "")
            # Weapons have their own event type and are expended in large numbers.
            if unit_class.strip().lower() in {"weapon", "missile", "guidedweapon", "unguidedweapon"} or unit_class in {"武器", "导弹"}:
                continue
            previous_status = self.loss_status.get(unit_id)
            self.loss_status[unit_id] = "confirmed"
            side_id = str(loss.get("SideID") or "")
            details = {
                "side": self.side_names.get(side_id, side_id or "未知阵营"),
                "side_id": side_id,
                "observed_by": agent.Name,
                "simulation_seconds": simulation_seconds,
                "unit_id": unit_id,
                "unit_name": loss.get("UnitName") or unit_id,
                "unit_class": unit_class,
                "status": "confirmed",
                "source": "unit_destroyed_event",
                "reason": loss.get("Reason") or "",
                "cause": loss.get("Cause") or "",
                "destroy_type": loss.get("DestroyType"),
                "damage_percent": loss.get("DamagePercent"),
            }
            if self._round(agent):
                details["round"] = self._round(agent)
            self.record("unit_loss_confirmed" if previous_status == "unconfirmed" else "unit_loss",
                        **details)

        if situation is None:
            return
        current = self._units(situation)
        previous = self.snapshots.get(agent.Name)
        self.snapshots[agent.Name] = current
        if previous is None:
            return
        for unit_id, unit in current.items():
            if self.loss_status.get(unit_id) == "unconfirmed":
                del self.loss_status[unit_id]
                self.record("unit_loss_retracted", side=agent.Name,
                            simulation_seconds=simulation_seconds,
                            unit_id=unit_id, unit_name=unit.get("name") or unit_id)
        for unit_id, unit in previous.items():
            if unit_id in current or unit_id in self.loss_status:
                continue
            self.loss_status[unit_id] = "unconfirmed"
            details = {
                "side": agent.Name,
                "side_id": str(getattr(agent, "ID", "") or ""),
                "observed_by": agent.Name,
                "simulation_seconds": simulation_seconds,
                "unit_id": unit_id,
                "unit_name": unit.get("name") or unit_id,
                "unit_class": unit.get("category") or "",
                "status": "unconfirmed",
                "source": "side_situation_missing",
            }
            if self._round(agent):
                details["round"] = self._round(agent)
            self.record("unit_loss", **details)


def with_loss_reporting(agent_class, monitor):
    """Observe events before an agent may return early from its decision step."""

    class LossReportingAgent(agent_class):
        def initial(self, situation):
            monitor.register_side(self)
            monitor.register_situation(self, situation)
            return super().initial(situation)

        def step(self, time_elapse, situation):
            monitor.observe(self, time_elapse, situation)
            return super().step(time_elapse, situation)

        def deduction_end(self):
            monitor.observe(self, getattr(self, "time_elapse", None))
            return super().deduction_end()

    LossReportingAgent.__name__ = agent_class.__name__
    return LossReportingAgent
