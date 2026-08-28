# Regenerates the speech fixtures. Run from the repository root.
$dir = Join-Path $PSScriptRoot ""
Add-Type -AssemblyName System.Speech
$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)
$cases = @{
  "simple"   = "The quick brown fox jumps over the lazy dog."
  "two_part" = 'First sentence here. <break time="1500ms"/> Second sentence follows.'
  "numbers"  = "I need twenty five widgets by Friday afternoon."
}
foreach ($k in $cases.Keys) {
  $s = New-Object System.Speech.Synthesis.SpeechSynthesizer
  $s.SetOutputToWaveFile("$dir\$k.wav", $fmt)
  if ($cases[$k] -like "*<break*") {
    $ssml = '<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" xml:lang="en-US">' + $cases[$k] + '</speak>'
    $s.SpeakSsml($ssml)
  } else { $s.Speak($cases[$k]) }
  $s.Dispose()
}
