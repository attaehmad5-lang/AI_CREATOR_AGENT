"""240-feature master checklist for the Unified Provider Registry.

Evidence rule: a feature is VERIFIED only when at least one test tagged with
``@covers("<ID>")`` actually passed in the most recent run. Nothing here is
hand-marked complete.
"""
from __future__ import annotations

from typing import Callable, Iterable

SECTIONS: dict[str, tuple[str, list[str]]] = {
    "A": ("Foundation & Core Architecture", [
        "Unified Registry Core Engine", "Central Provider Catalog", "Provider Registration System",
        "Provider Identification System", "Unique Provider ID Generator", "Provider Name Management",
        "Provider Metadata Management", "Provider Description Management", "Provider Category Management",
        "Registry Initialization Engine", "Registry Configuration Loader", "Registry State Manager",
        "Registry Lifecycle Manager", "Registry Shutdown Manager", "Registry Version Management"]),
    "B": ("Provider Registration & Discovery", [
        "Automatic Provider Registration", "Manual Provider Registration", "Dynamic Provider Discovery",
        "Automatic Provider Detection", "Provider Import Management", "Provider Module Discovery",
        "Provider Package Discovery", "Provider Class Discovery", "Provider Interface Detection",
        "Duplicate Provider Detection", "Provider ID Conflict Detection", "Provider Registration Validation",
        "Provider Registration Rollback", "Bulk Provider Registration", "Provider Registration History"]),
    "C": ("Provider Catalog & Organization", [
        "Image Provider Catalog", "Video Provider Catalog", "Voice Provider Catalog", "Music Provider Catalog",
        "Text Provider Catalog", "Animation Provider Catalog", "Audio Processing Provider Catalog",
        "Document Generation Provider Catalog", "Research Provider Catalog", "Custom Provider Catalog",
        "Multi-Category Provider Support", "Provider Tag Management", "Provider Group Management",
        "Provider Classification Engine", "Provider Catalog Search Engine"]),
    "D": ("Provider Configuration Management", [
        "Central Configuration Manager", "Provider-Specific Configuration", "Default Configuration Management",
        "Configuration Inheritance", "Configuration Validation", "Configuration Schema Management",
        "Environment-Based Configuration", "Configuration Profiles", "Configuration Versioning",
        "Configuration Change Detection", "Configuration Rollback", "Configuration Import/Export",
        "Runtime Configuration Updates", "Configuration Conflict Resolution", "Configuration Integrity Verification"]),
    "E": ("Provider Lifecycle Management", [
        "Provider Loading Engine", "Provider Unloading Engine", "Provider Enable/Disable System",
        "Provider Activation Manager", "Provider Deactivation Manager", "Provider Restart Manager",
        "Provider Reload Manager", "Provider Initialization Validation", "Provider Shutdown Validation",
        "Provider Lifecycle State Tracking", "Provider Dependency Management", "Dependency Resolution Engine",
        "Dependency Conflict Detection", "Circular Dependency Detection", "Automatic Dependency Recovery"]),
    "F": ("Unified Provider Interface", [
        "Standard Provider Interface", "Unified Provider Contract", "Provider Base Class",
        "Interface Compatibility Validation", "Standardized Input Schema", "Standardized Output Schema",
        "Unified Error Response Format", "Standardized Generation Methods", "Provider Capability Interface",
        "Provider Authentication Interface", "Provider Health Interface", "Provider Configuration Interface",
        "Provider Event Interface", "Interface Version Compatibility", "Legacy Provider Adapter Support"]),
    "G": ("Provider Capabilities & Intelligence", [
        "Automatic Capability Detection", "Capability Registration", "Capability Verification",
        "Capability-Based Provider Matching", "Model Discovery", "Model Metadata Management",
        "Model Version Tracking", "Model Availability Detection", "Model Compatibility Checking",
        "Input Format Compatibility", "Output Format Compatibility", "Resolution Capability Management",
        "Duration Capability Management", "Quality-Level Capability Management", "Advanced Capability Filtering"]),
    "H": ("Provider Health & Reliability", [
        "Provider Health Monitoring", "Automatic Health Checks", "Live Connectivity Verification",
        "Provider Availability Monitoring", "Provider Performance Monitoring", "Provider Response-Time Tracking",
        "Provider Failure Detection", "Provider Failure Classification", "Provider Reliability Scoring",
        "Provider Health History", "Automatic Unhealthy Provider Isolation", "Automatic Recovery Detection",
        "Health-Based Provider Eligibility", "Provider Degradation Detection", "Registry-Wide Health Dashboard"]),
    "I": ("Security & Credential Protection", [
        "Secure Credential Reference Management", "API Key Isolation", "Secret Encryption at Rest",
        "Secure Credential Access", "Environment Secret Integration", "Credential Expiration Detection",
        "Credential Rotation Support", "Credential Permission Validation", "Provider Authentication Validation",
        "Secure TLS Verification", "Certificate Validation", "Secret Leakage Prevention",
        "Credential Access Audit Logs", "Provider Permission Isolation", "Security Policy Enforcement"]),
    "J": ("Intelligent Routing & Selection", [
        "Central Provider Selection Engine", "Capability-Based Routing", "Health-Based Routing",
        "Performance-Based Routing", "Quality-Based Routing", "Cost-Aware Provider Selection",
        "Priority-Based Provider Selection", "Primary Provider Management", "Secondary Provider Management",
        "Automatic Fallback Integration", "Multi-Provider Routing", "Intelligent Load Balancing",
        "Provider Availability-Based Selection", "Dynamic Provider Re-Ranking", "Routing Decision Audit Trail"]),
    "K": ("Enterprise Performance & Scalability", [
        "Asynchronous Provider Registration", "Concurrent Provider Loading", "Thread-Safe Registry Operations",
        "Async Registry API", "Registry Caching Engine", "Intelligent Cache Invalidation", "Lazy Provider Loading",
        "On-Demand Provider Discovery", "Distributed Registry Support", "Multi-Worker Registry Synchronization",
        "Horizontal Scalability", "High-Availability Architecture", "Registry State Persistence",
        "High-Performance Lookup Engine", "Resource Usage Optimization"]),
    "L": ("Monitoring, Events & Diagnostics", [
        "Central Registry Event System", "Provider Lifecycle Events", "Provider Registration Events",
        "Provider Failure Events", "Provider Recovery Events", "Event Subscription Management",
        "Event Notification System", "Structured Logging", "Registry Audit Trail",
        "Performance Metrics Collection", "Registry Diagnostics Engine", "Automatic Error Reporting",
        "Registry Status API", "Monitoring Dashboard Integration", "Alert & Notification Integration"]),
    "M": ("Advanced Validation & Quality Control", [
        "Provider Schema Validation", "Provider Contract Testing", "Provider Compatibility Testing",
        "Provider Integration Testing", "Provider Registration Testing", "Provider Lifecycle Testing",
        "Provider Security Testing", "Provider Failure Simulation", "Registry Stress Testing",
        "Concurrent Operation Testing", "Dependency Failure Testing", "Regression Testing",
        "Automated Test Coverage Analysis", "Provider Certification System", "Registry Quality Gate"]),
    "N": ("Recovery, Backup & Disaster Management", [
        "Registry Automatic Backup", "Registry State Recovery", "Provider Metadata Recovery",
        "Configuration Recovery", "Registration History Recovery", "Crash Recovery",
        "Registry Snapshot Management", "Snapshot Integrity Verification", "Backup Version Management",
        "Disaster Recovery Mode", "Registry Restore Validation", "Corrupted Registry Detection",
        "Automatic Registry Repair", "Recovery Audit Logging", "Disaster Recovery Testing"]),
    "O": ("API, Integration & Ecosystem", [
        "Unified Registry API", "Provider Lookup API", "Provider Registration API", "Provider Management API",
        "Provider Health API", "Provider Capability API", "Registry Query API", "Registry Event API",
        "REST API Integration", "Internal SDK Integration", "Plugin System Integration",
        "Third-Party Provider Integration", "Custom Provider Adapter SDK", "External Provider Package Support",
        "API Authentication & Authorization"]),
    "P": ("Enterprise Governance & Advanced Intelligence", [
        "Multi-Tenant Registry Isolation", "Tenant-Specific Provider Catalogs", "Role-Based Access Control",
        "Provider Access Policies", "Provider Usage Restrictions", "Provider Quota Integration",
        "Provider Cost Tracking Integration", "Provider Compliance Policies", "Provider Approval Workflow",
        "Provider Deprecation Management", "Provider Retirement Management", "Provider Migration Management",
        "Registry Governance Rules", "Policy-Based Provider Selection", "Automated Registry Optimization"]),
}

FEATURES: dict[str, str] = {
    f"{letter}{index:02d}": name
    for letter, (_title, names) in SECTIONS.items()
    for index, name in enumerate(names, start=1)
}
assert len(FEATURES) == 240 and len(SECTIONS) == 16 and all(len(n) == 15 for _t, n in SECTIONS.values())

# Feature ID -> test ids that claim to cover it (populated at import time by @covers).
COVERAGE: dict[str, set[str]] = {}


def covers(*feature_ids: str) -> Callable:
    """Tag a test function/method with the feature IDs it provides evidence for."""
    for fid in feature_ids:
        if fid not in FEATURES:
            raise KeyError(f"unknown feature id {fid}")

    def decorator(func: Callable) -> Callable:
        existing = tuple(getattr(func, "_covers", ()))
        func._covers = existing + tuple(feature_ids)  # type: ignore[attr-defined]
        return func

    return decorator


def collect_coverage(test_classes: Iterable[type]) -> dict[str, set[str]]:
    """Map feature ID -> set of 'Class.method' names that are tagged for it."""
    mapping: dict[str, set[str]] = {}
    for cls in test_classes:
        for attr in dir(cls):
            func = getattr(cls, attr, None)
            for fid in getattr(func, "_covers", ()):
                mapping.setdefault(fid, set()).add(f"{cls.__name__}.{attr}")
    return mapping
