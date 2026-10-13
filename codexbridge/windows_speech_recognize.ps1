param(
    [Parameter(Mandatory = $true)][string]$WavPath,
    [string]$Locale = "zh-CN",
    [double]$MinConfidence = 0.0
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

# 音频预处理: 解析 WAV -> 去直流 -> 去掉首尾静音 -> 归一化 -> 写回 16k/单声道/16bit WAV。
# 设备录下来的原始音频常常前面有近 1 秒静音、后面带点击噪声, 直接丢给
# System.Speech 会让识别器抓错片段。这里先把有效语音段裁出来再识别。
$csharp = @"
using System;
using System.IO;
using System.Text;

public static class SpeechAudio
{
    public static string Prepare(string inPath, string outPath)
    {
        byte[] bytes = File.ReadAllBytes(inPath);
        if (bytes.Length < 44) throw new Exception("录音文件为空");
        if (Encoding.ASCII.GetString(bytes, 0, 4) != "RIFF" ||
            Encoding.ASCII.GetString(bytes, 8, 4) != "WAVE")
            throw new Exception("录音文件格式不正确");
        int pos = 12, channels = 0, sampleRate = 0, bits = 0, dataStart = -1, dataLen = 0;
        while (pos + 8 <= bytes.Length)
        {
            string id = Encoding.ASCII.GetString(bytes, pos, 4);
            int len = (int)BitConverter.ToUInt32(bytes, pos + 4);
            int body = pos + 8;
            if (id == "fmt ")
            {
                int fmt = BitConverter.ToUInt16(bytes, body);
                channels = BitConverter.ToUInt16(bytes, body + 2);
                sampleRate = (int)BitConverter.ToUInt32(bytes, body + 4);
                bits = BitConverter.ToUInt16(bytes, body + 14);
                if (fmt != 1 || bits != 16) throw new Exception("录音编码不受支持");
            }
            else if (id == "data")
            {
                dataStart = body;
                dataLen = len;
                if (dataStart + dataLen > bytes.Length) dataLen = bytes.Length - dataStart;
            }
            pos = body + len + (len % 2);
        }
        if (dataStart < 0 || dataLen <= 0 || channels <= 0 || sampleRate <= 0)
            throw new Exception("录音文件缺少音频数据");

        int count = dataLen / 2;
        int frames = count / channels;
        if (frames <= 0) throw new Exception("录音没有采样点");
        double[] samples = new double[frames];
        for (int i = 0; i < frames; i++)
        {
            double sum = 0;
            for (int c = 0; c < channels; c++)
                sum += BitConverter.ToInt16(bytes, dataStart + (i * channels + c) * 2);
            samples[i] = sum / channels;
        }

        double mean = 0;
        for (int i = 0; i < frames; i++) mean += samples[i];
        mean /= frames;
        for (int i = 0; i < frames; i++) samples[i] -= mean;

        int frame = Math.Max(1, sampleRate / 50);
        int nf = frames / frame;
        if (nf < 1) nf = 1;
        double[] rms = new double[nf];
        double peakRms = 0;
        for (int f = 0; f < nf; f++)
        {
            double s = 0;
            int b = f * frame;
            int lim = Math.Min(frame, frames - b);
            for (int i = 0; i < lim; i++) { double v = samples[b + i]; s += v * v; }
            rms[f] = Math.Sqrt(s / Math.Max(1, lim));
            if (rms[f] > peakRms) peakRms = rms[f];
        }
        double threshold = Math.Max(peakRms * 0.05, 120.0);
        int first = 0, last = nf - 1;
        while (first < nf && rms[first] <= threshold) first++;
        while (last >= 0 && rms[last] <= threshold) last--;
        int pad = sampleRate * 120 / 1000;
        int start = 0, end = frames;
        if (last >= first)
        {
            start = Math.Max(0, first * frame - pad);
            end = Math.Min(frames, (last + 1) * frame + pad);
        }
        int segLen = end - start;
        if (segLen <= 0) { start = 0; end = frames; segLen = frames; }
        double[] seg = new double[segLen];
        Array.Copy(samples, start, seg, 0, segLen);

        double pk = 0;
        for (int i = 0; i < segLen; i++) { double a = Math.Abs(seg[i]); if (a > pk) pk = a; }
        if (pk > 0)
        {
            double gain = (32767.0 * 0.9) / pk;
            if (gain > 1.0) for (int i = 0; i < segLen; i++) seg[i] *= gain;
        }

        int byteCount = segLen * 2;
        using (FileStream fs = new FileStream(outPath, FileMode.Create, FileAccess.Write))
        using (BinaryWriter bw = new BinaryWriter(fs))
        {
            bw.Write(Encoding.ASCII.GetBytes("RIFF"));
            bw.Write(36 + byteCount);
            bw.Write(Encoding.ASCII.GetBytes("WAVE"));
            bw.Write(Encoding.ASCII.GetBytes("fmt "));
            bw.Write(16);
            bw.Write((ushort)1);
            bw.Write((ushort)1);
            bw.Write(sampleRate);
            bw.Write(sampleRate * 2);
            bw.Write((ushort)2);
            bw.Write((ushort)16);
            bw.Write(Encoding.ASCII.GetBytes("data"));
            bw.Write(byteCount);
            for (int i = 0; i < segLen; i++)
            {
                double v = Math.Round(seg[i]);
                if (v > 32767) v = 32767;
                if (v < -32768) v = -32768;
                bw.Write((short)v);
            }
        }
        return string.Format(System.Globalization.CultureInfo.InvariantCulture,
            "fmt {0}Hz ch{1} frames={2} speech={3:F0}ms..{4:F0}ms peak={5:F0}",
            sampleRate, channels, frames, start * 1000.0 / sampleRate, end * 1000.0 / sampleRate, pk);
    }
}
"@

function Get-BestRecognition([string]$path, [System.Globalization.CultureInfo]$culture) {
    $engine = $null
    try {
        $engine = [System.Speech.Recognition.SpeechRecognitionEngine]::new($culture)
        $engine.MaxAlternates = 5
        $engine.LoadGrammar([System.Speech.Recognition.DictationGrammar]::new())
        $engine.SetInputToWaveFile($path)
        $result = $engine.Recognize()
        if ($null -eq $result) { return $null }
        $bestText = $result.Text
        $bestConf = $result.Confidence
        foreach ($alt in $result.Alternates) {
            if ($alt.Confidence -gt $bestConf -and -not [string]::IsNullOrWhiteSpace($alt.Text)) {
                $bestText = $alt.Text
                $bestConf = $alt.Confidence
            }
        }
        return [pscustomobject]@{ Text = $bestText; Confidence = $bestConf }
    }
    finally {
        if ($null -ne $engine) { $engine.Dispose() }
    }
}

try {
    Add-Type -TypeDefinition $csharp -Language CSharp | Out-Null
    Add-Type -AssemblyName System.Speech
    $culture = [System.Globalization.CultureInfo]::GetCultureInfo($Locale)

    $processed = [System.IO.Path]::Combine([System.IO.Path]::GetTempPath(), "folos-speech-" + [Guid]::NewGuid().ToString("N") + ".wav")
    try {
        $info = [SpeechAudio]::Prepare($WavPath, $processed)
        [Console]::Error.WriteLine("预处理: " + $info)
    }
    catch {
        $processed = $null
        [Console]::Error.WriteLine("预处理失败, 使用原始音频: " + $_.Exception.Message)
    }

    $best = $null
    $candidates = @()
    if ($processed) { $candidates += $processed }
    $candidates += $WavPath
    foreach ($candidate in $candidates) {
        try {
            $r = Get-BestRecognition $candidate $culture
            if ($null -ne $r -and -not [string]::IsNullOrWhiteSpace($r.Text)) {
                if ($null -eq $best -or $r.Confidence -gt $best.Confidence) { $best = $r }
            }
        }
        catch {
            [Console]::Error.WriteLine("识别尝试失败: " + $_.Exception.Message)
        }
    }

    if ($null -eq $best -or [string]::IsNullOrWhiteSpace($best.Text)) {
        throw "未识别到文字，请靠近设备后重新录音"
    }
    if ($best.Confidence -lt $MinConfidence) {
        throw "没听清，请靠近设备后再说一遍"
    }
    Write-Output $best.Text
}
catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
}
finally {
    if ($processed -and (Test-Path $processed)) { Remove-Item -LiteralPath $processed -Force -ErrorAction SilentlyContinue }
}
