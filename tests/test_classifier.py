from app.skills.classifier import classify
from app.skills.classifier.governance_policy import GovernancePolicyDetector
from app.skills.classifier.argocd_appset import ArgocdAppSetDetector


POLICY_DIFF = """\
--- a/policies/etcd-encryption.yaml
+++ b/policies/etcd-encryption.yaml
@@ -0,0 +1,20 @@
+apiVersion: policy.open-cluster-management.io/v1
+kind: Policy
+metadata:
+  name: policy-etcd-encryption
+  namespace: policies
+spec:
+  remediationAction: enforce
+  policy-templates:
+    - objectDefinition:
+        kind: ConfigPolicy
+        spec:
+          remediationAction: enforce
"""

POLICY_WITH_BINDING_DIFF = POLICY_DIFF + """\
+---
+apiVersion: policy.open-cluster-management.io/v1
+kind: PlacementBinding
+metadata:
+  name: policy-etcd-encryption-binding
+placementRef:
+  name: placement-prod
+subjects:
+  - name: policy-etcd-encryption
"""

APPSET_DIFF = """\
--- a/appsets/monitoring.yaml
+++ b/appsets/monitoring.yaml
@@ -0,0 +1,15 @@
+apiVersion: argoproj.io/v1alpha1
+kind: ApplicationSet
+metadata:
+  name: platform-monitoring
+spec:
+  generators:
+    - clusterDecisionResource:
+        configMapRef: acm-placement
+        labelSelector:
+          matchLabels:
+            env: production
"""

PLAIN_DIFF = """\
--- a/README.md
+++ b/README.md
@@ -1 +1 @@
-old line
+new line
"""


# GovernancePolicyDetector
def test_governance_detects_policy():
    detector = GovernancePolicyDetector()
    result = detector.detect(POLICY_DIFF, [])
    assert result is not None
    assert result.kind == "governance-policy"
    assert "Policy" in result.resources
    assert result.has_placement is False


def test_governance_detects_placement_binding():
    detector = GovernancePolicyDetector()
    result = detector.detect(POLICY_WITH_BINDING_DIFF, [])
    assert result is not None
    assert result.has_placement is True


def test_governance_no_match_plain_diff():
    detector = GovernancePolicyDetector()
    assert detector.detect(PLAIN_DIFF, []) is None


# ArgocdAppSetDetector
def test_argocd_detects_appset():
    detector = ArgocdAppSetDetector()
    result = detector.detect(APPSET_DIFF, [])
    assert result is not None
    assert result.kind == "argocd-appset"
    assert "ApplicationSet" in result.resources
    assert result.has_placement is True  # clusterDecisionResource present


def test_argocd_no_match_plain_diff():
    detector = ArgocdAppSetDetector()
    assert detector.detect(PLAIN_DIFF, []) is None


# classify() registry
def test_classify_governance_pr():
    results = classify(POLICY_DIFF, [])
    assert len(results) == 1
    assert results[0].kind == "governance-policy"


def test_classify_argocd_pr():
    results = classify(APPSET_DIFF, [])
    assert len(results) == 1
    assert results[0].kind == "argocd-appset"


def test_classify_unrecognised_pr():
    results = classify(PLAIN_DIFF, [])
    assert results == []


def test_classify_multi_type_pr():
    combined = POLICY_DIFF + "\n" + APPSET_DIFF
    results = classify(combined, [])
    kinds = {r.kind for r in results}
    assert "governance-policy" in kinds
    assert "argocd-appset" in kinds
