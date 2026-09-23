"""A-owned B1 WorkflowSpec using real B Catalog schemas, no B internals.

The six-step chain is explicit A workflow policy. `$` output binding copies the
entire JSON Tool result to A state for the next Tool's nested input. B still
owns each Tool's validation, execution, trace and EventBus sequence.
"""

from runtime.workflow import WorkflowSpec


def _obj(fields, required=()):
    return {
        "type": "object", "properties": fields, "required": list(required),
        "additionalProperties": False,
    }


def _ref(path):
    return {"type": "ref", "path": path}


def build_real_b1_workflow() -> WorkflowSpec:
    """Caller-owned six-Tool Golden Path, run in both deterministic B scenarios."""
    names = [
        "query_tbm_status", "query_sensor_history", "detect_parameter_anomaly",
        "query_geological_data", "diagnose_fault", "estimate_risk",
    ]
    nodes = [
        {"id": "status", "name": "Status", "kind": "capability", "capability": names[0],
         "inputs": {"machine_id": _ref("$.inputs.machine_id")},
         "outputs": {"$": "$.data.current_status"}},
        {"id": "history", "name": "Sensor history", "kind": "capability", "capability": names[1],
         "inputs": {"machine_id": _ref("$.inputs.machine_id")},
         "outputs": {"$": "$.data.sensor_history"}},
        {"id": "anomaly", "name": "Anomaly", "kind": "capability", "capability": names[2],
         "inputs": {"machine_id": _ref("$.inputs.machine_id"),
                    "current_status": _ref("$.data.current_status"),
                    "history": _ref("$.data.sensor_history.records")},
         "outputs": {"$": "$.data.anomaly"}},
        {"id": "geology", "name": "Geology", "kind": "capability", "capability": names[3],
         "inputs": {"machine_id": _ref("$.inputs.machine_id")},
         "outputs": {"$": "$.data.geology"}},
        {"id": "diagnosis", "name": "Diagnosis", "kind": "capability", "capability": names[4],
         "inputs": {"machine_id": _ref("$.inputs.machine_id"),
                    "anomalies": _ref("$.data.anomaly.anomalies"),
                    "geology": _ref("$.data.geology")},
         "outputs": {"$": "$.data.diagnosis"}},
        {"id": "risk", "name": "Risk", "kind": "capability", "capability": names[5],
         "inputs": {"diagnosis": _ref("$.data.diagnosis")},
         "outputs": {"$": "$.data.risk"}},
    ]
    # A's static state schema supports a small dialect; the complete B Tool
    # schemas remain intact in CatalogRegistry and are validated at each Tool.
    open_object = {"type": "object"}
    return WorkflowSpec.model_validate({
        "spec_version": "1.1", "id": "real_b1", "version": "1", "name": "Real B1 provider chain",
        "entrypoint": "status", "input_schema": _obj({"machine_id": {"type": "string"}}, ["machine_id"]),
        "state_schema": _obj({
            "current_status": open_object, "sensor_history": open_object,
            "anomaly": open_object, "geology": open_object,
            "diagnosis": open_object, "risk": open_object,
        }, ["current_status", "sensor_history", "anomaly", "geology", "diagnosis", "risk"]),
        "nodes": nodes,
        "edges": [{"source": nodes[i]["id"], "target": nodes[i + 1]["id"]}
                  for i in range(len(nodes) - 1)]
        + [{"source": "risk", "target": "$end"}],
    })
