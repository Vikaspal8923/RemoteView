# Affinity capture verification

| Acceptance item | Status | Reason |
|---|---|---|
| Protected content readback | Unverified-with-reason | Requires an interactive Windows desktop and D3D11 shared surface |
| Z-order occlusion | Verified on Linux | Pure rectangle subtraction and compositor tests |
| No-protected-window compatibility | Verified on Linux | Existing capture tests pass where dependencies are installed |
| Minimize/delete safety | Unverified-with-reason | Requires live Win32 window lifecycle |
| Diagnostic affinity reporting | Unverified-with-reason | Requires Win32 user32 |
| Missing DWM export fallback | Verified on Linux | Lazy API handles safely become unavailable |
| Hybrid adapter selection | Unverified-with-reason | Requires Windows DXGI adapter enumeration |
| Device-removed recovery | Unverified-with-reason | Requires live D3D11 device fault |
