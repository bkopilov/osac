# CUDN EVPN/Netris E2E profile

This is the explicit opt-in installer profile for the Phase 1 IPv4 CUDN EVPN
environment. It is intentionally separate from the base chart and all normal
profiles.

The profile provides:

- the normal CaaS infrastructure and OSAC instance values;
- `netris` as the fabric manager and `cudn_evpn` as the k8s manager;
- a default `NetworkClass` with `fabricManager: netris` and
  `k8sManager: cudn_evpn`;
- the standard Netris AAP instance-group configuration needed by the cluster
  and network fulfillment jobs.

Before installing, the target OpenShift cluster must already have the Phase 1
EVPN/BGP/VTEP prerequisites and the `cudn_evpn` implementation. This profile
only registers and selects the managers; it does not provision that external
fabric or implement the manager.

Netris passwords, SSH keys, and site-specific values must be supplied through
a private values file. From `osac-installer/`:

```bash
make install PLATFORM=openshift PROFILE=cudn-evpn-netris-test NS=osac \
  INSTANCE_VALUES_EXTRA="-f cudn-evpn-netris-test-secrets.local.yaml"
```

The Makefile profile selection is the installation wiring for this profile;
the standard CI workflows continue to use their existing profiles.
