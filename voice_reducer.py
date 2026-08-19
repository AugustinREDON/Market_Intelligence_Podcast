import torchaudio

wav, sr = torchaudio.load("voices/host_b.wav")

# Reduce Host B volume
wav = wav * 0.6

torchaudio.save("voices/host_b_quieter.wav", wav, sr)

print("Created host_b_quieter.wav")