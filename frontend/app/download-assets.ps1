# Downloads the ENCORE images and icons (every ENCORE page) from Figma into .\assets
# Run once from this folder (frontend\app):  powershell -ExecutionPolicy Bypass -File .\download-assets.ps1
# The Figma links are temporary (valid about 7 days from 27 Oct 2026). If they expire, ask for a fresh script.

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$dir = Join-Path $here "assets"
New-Item -ItemType Directory -Force -Path $dir | Out-Null

$assets = [ordered]@{
  # photography
  "hero.png"                 = "https://www.figma.com/api/mcp/asset/8097e08e-ae3b-4670-a525-0e318fb2fd9b.png"
  "concert-1.png"            = "https://www.figma.com/api/mcp/asset/56450346-3819-4959-923f-9b455a55ceb5.png"
  "concert-2.png"            = "https://www.figma.com/api/mcp/asset/b60758f2-827e-4d34-8ac1-1727fb38bd84.png"
  "concert-3.png"            = "https://www.figma.com/api/mcp/asset/2efda331-76ba-419b-b7a8-c0b73c603fa3.png"
  "concert-4.png"            = "https://www.figma.com/api/mcp/asset/4bb03cb6-b35b-49ca-8291-9735878410f4.png"
  "spotlight.png"            = "https://www.figma.com/api/mcp/asset/b69380aa-358c-4bae-b083-e36695c3e1ac.png"
  "artist-1.png"             = "https://www.figma.com/api/mcp/asset/f34dcbb8-81d8-4a88-be16-d49d2ce999e2.png"
  "artist-2.png"             = "https://www.figma.com/api/mcp/asset/db47fa4b-12ae-4959-bfa3-f8a693f52e2e.png"
  "artist-3.png"             = "https://www.figma.com/api/mcp/asset/e103fccb-98cf-4622-87b8-44b478f45f39.png"
  "artist-4.png"             = "https://www.figma.com/api/mcp/asset/adf14a8a-d949-4701-809f-fcaba8aa9a0f.png"
  "artist-5.png"             = "https://www.figma.com/api/mcp/asset/d512de67-7ad8-4884-b93a-898ca8d80098.png"
  # icons
  "audio-lines.svg"          = "https://www.figma.com/api/mcp/asset/3c6484bf-cb4f-4558-8f94-ac58de7a2d1d.svg"
  "audio-lines-light.svg"    = "https://www.figma.com/api/mcp/asset/addf3e5f-9658-4ce0-b824-fbdf12897031.svg"
  "map-pin.svg"              = "https://www.figma.com/api/mcp/asset/34e80c77-58c3-48bb-a2d4-489e56eb82df.svg"
  "arrow-right-dark.svg"     = "https://www.figma.com/api/mcp/asset/6f9d45fe-f3cf-48a0-93df-46466a611db5.svg"
  "arrow-left-white.svg"     = "https://www.figma.com/api/mcp/asset/ba04504e-4091-4dd5-9d01-6b00708e86a7.svg"
  "arrow-right-white-sm.svg" = "https://www.figma.com/api/mcp/asset/ddb94e2a-5081-405b-8ca2-9b17e4a64630.svg"
  "search.svg"               = "https://www.figma.com/api/mcp/asset/a6fe19b7-cf80-4ad8-b312-9d2d9d4d65ab.svg"
  "chevron-down.svg"         = "https://www.figma.com/api/mcp/asset/925c86a6-fc0c-43ec-a077-f6be90123a07.svg"
  "arrow-right-white.svg"    = "https://www.figma.com/api/mcp/asset/ec5987c4-3d5e-4406-aa56-5f51f9425b32.svg"
  "arrow-right-purple.svg"   = "https://www.figma.com/api/mcp/asset/f3ce5bba-d31e-4b21-97a0-39a23e34f1d2.svg"
  "heart.svg"                = "https://www.figma.com/api/mcp/asset/9d80b7bb-863a-4832-b04e-30ed975a78a0.svg"
  "bell.svg"                 = "https://www.figma.com/api/mcp/asset/855d630e-19b5-48d9-87e1-8555a69084da.svg"
  "shield-check.svg"         = "https://www.figma.com/api/mcp/asset/9e78dec4-f8f0-4d24-896c-338dbf5fbaa7.svg"
  "scan-line.svg"            = "https://www.figma.com/api/mcp/asset/44e4ab8c-4fc4-4ede-a2fc-47c228f1deb7.svg"
  "headphones.svg"           = "https://www.figma.com/api/mcp/asset/ead6bf06-be96-4a1a-81af-111b1adeb617.svg"
  # concert details page (event.html)
  "event-hero.png"           = "https://www.figma.com/api/mcp/asset/6703e971-24a4-4f73-9b74-be57bc7db3c4.png"
  "clock.svg"                = "https://www.figma.com/api/mcp/asset/6eda53be-683f-47cd-afc3-5b90e1861196.svg"
  "users.svg"                = "https://www.figma.com/api/mcp/asset/9a27a27c-a8e3-46f6-99a5-4c1ab0241449.svg"
  "ticket.svg"               = "https://www.figma.com/api/mcp/asset/30cf0f79-ec92-400f-8c80-3202320b2ae8.svg"
  "map-pin-purple.svg"       = "https://www.figma.com/api/mcp/asset/d3427f8c-f2fe-4551-8994-4510b7792931.svg"
  "plus.svg"                 = "https://www.figma.com/api/mcp/asset/c7bdff03-b587-4f10-a8c9-179c3b3cfa24.svg"
  # seat selection page (seats.html)
  "seats-photo.png"          = "https://www.figma.com/api/mcp/asset/41fd4ef7-660e-4bb7-8af3-0ba79dc0f25a.png"
  "minus.svg"                = "https://www.figma.com/api/mcp/asset/b21d249f-4af6-4ef6-b3e5-1ed5aaa37d51.svg"
  "maximize.svg"             = "https://www.figma.com/api/mcp/asset/4d0073d2-2ec5-4ca0-b2a7-521aff28a97a.svg"
  "accessibility.svg"        = "https://www.figma.com/api/mcp/asset/92c59155-3483-4d40-bdec-dfe4cff2f066.svg"
  # checkout page (checkout.html)
  "lock.svg"                 = "https://www.figma.com/api/mcp/asset/221fc4a4-aee3-4544-a6ae-6d03f7ce1482.svg"
  "lock-purple.svg"          = "https://www.figma.com/api/mcp/asset/1a6904a3-7ab6-4f00-ae5f-267ce3d2920c.svg"
  "lock-white.svg"           = "https://www.figma.com/api/mcp/asset/3c5f16b6-966f-4ff2-83a8-0a91379fbba1.svg"
  "user-round.svg"           = "https://www.figma.com/api/mcp/asset/3baaebd8-9ab1-429c-8e67-6929c77dd633.svg"
  "mail.svg"                 = "https://www.figma.com/api/mcp/asset/d6aafa5a-733a-43e6-b95a-1c53560e1cc8.svg"
  "credit-card.svg"          = "https://www.figma.com/api/mcp/asset/31a63fdd-c990-402b-b5ae-ff49c28dbab6.svg"
  "circle-help.svg"          = "https://www.figma.com/api/mcp/asset/7a9467a3-ce44-4328-9a0c-419e7ed00563.svg"
  # log in + registration (login.html, register.html)
  "auth-crowd.png"           = "https://www.figma.com/api/mcp/asset/3473a321-1cab-4ab5-922c-b6278fe0b45d.png"
  "eye-off.svg"              = "https://www.figma.com/api/mcp/asset/b10b9515-dc50-4709-bdd6-377af4896df1.svg"
}

$failed = @()
foreach ($name in $assets.Keys) {
  $out = Join-Path $dir $name
  try {
    Invoke-WebRequest -Uri $assets[$name] -OutFile $out -UseBasicParsing
    $size = (Get-Item $out).Length
    if ($size -eq 0) { throw "empty file" }
    Write-Host ("OK   {0,-26} {1,8:N0} bytes" -f $name, $size)
  } catch {
    Write-Host ("FAIL {0,-26} {1}" -f $name, $_.Exception.Message) -ForegroundColor Red
    $failed += $name
  }
}

if ($failed.Count -eq 0) {
  Write-Host "`nAll $($assets.Count) assets downloaded to $dir" -ForegroundColor Green
} else {
  Write-Host "`n$($failed.Count) asset(s) failed: $($failed -join ', ')" -ForegroundColor Red
  exit 1
}
