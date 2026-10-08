"""One publication policy for immediate, scheduled and retried listings."""

import json

from bit.infringement_state import canonical_record, result_state, review_is_current


class InfringementReviewRequired(ValueError):
    """Hold publication for infringement review."""


def publication_issues(row, site_id=None, rule_version=None):
    # Third-party callers predating the schema keep their existing contract.
    # Database rows always include these columns after the schema migration.
    if not any(key in row for key in ("infringement_check_json", "infringement_risk_level", "risk_level", "infringement")):
        return []
    record = canonical_record(row)
    review = record.get("manual_decision")
    if review == "block" and review_is_current(record, site_id=site_id):
        return ["侵权人工复核已拦截"]
    if review == "allow" and review_is_current(record):
        sites = record.get("review_site_ids") or []
        if isinstance(sites, str):
            try:
                sites = json.loads(sites)
            except ValueError:
                sites = []
        if not record.get("review_evidence_ref") or not record.get("review_expires_at") or not sites:
            return ["侵权放行缺少授权凭证、站点或有效期"]
        if site_id and site_id not in sites:
            return ["侵权放行不适用于目标站点"]
        return []
    state = result_state(record, rule_version=rule_version)
    if state == "unchecked":
        return ["侵权尚未检测"]
    if state == "stale":
        return ["侵权检测结果已失效，请重新检测"]
    if state == "failed":
        return ["侵权检测失败，请重试"]
    risk = record.get("risk_level")
    if risk == 2:
        return ["侵权检测为高风险，需人工复核"]
    if risk == 1:
        return ["侵权检测为疑似风险，需人工复核"]
    if risk != 0:
        return ["侵权检测结果无效"]
    return []


def validate_publication(row, site_id=None, rule_version=None):
    issues = publication_issues(row, site_id, rule_version)
    if issues:
        raise InfringementReviewRequired("；".join(issues))
