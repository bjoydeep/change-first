from app.skills.acm_placement import PlacementCase, parse_placement


NEW_PLACEMENT_DIFF = """\
+apiVersion: cluster.open-cluster-management.io/v1beta1
+kind: Placement
+metadata:
+  name: placement-prod
+  namespace: policies
+spec:
+  clusterSets:
+    - production
+  predicates:
+    - requiredClusterSelector:
+        labelSelector:
+          matchLabels:
+            env: production
+            region: us-east
"""

BINDING_ONLY_DIFF = """\
+apiVersion: policy.open-cluster-management.io/v1
+kind: PlacementBinding
+metadata:
+  name: policy-binding
+  namespace: policies
+placementRef:
+  name: existing-placement
+  kind: Placement
+subjects:
+  - name: my-policy
+    kind: Policy
"""

NO_PLACEMENT_DIFF = """\
+apiVersion: policy.open-cluster-management.io/v1
+kind: ConfigPolicy
+metadata:
+  name: check-etcd
"""


def test_new_placement_is_evaluate_case():
    result = parse_placement(NEW_PLACEMENT_DIFF)
    assert result.case == PlacementCase.EVALUATE
    assert result.placement_name != ""
    assert result.cluster_selector == {"env": "production", "region": "us-east"}


def test_binding_only_is_read_decision_case():
    result = parse_placement(BINDING_ONLY_DIFF)
    assert result.case == PlacementCase.READ_DECISION
    assert result.placement_name == "existing-placement"


def test_no_placement_is_absent():
    result = parse_placement(NO_PLACEMENT_DIFF)
    assert result.case == PlacementCase.ABSENT


def test_absent_returns_empty_name():
    result = parse_placement(NO_PLACEMENT_DIFF)
    assert result.placement_name == ""
    assert result.cluster_selector == {}
