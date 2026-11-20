# Third-party patches

## FireRedASR2S — fireredasr2/models/fireredasr_aed.py
`get_token_timestamp_torchaudio` passed bfloat16 CTC logits to
`torchaudio.functional.forced_align`, which only accepts fp16/fp32/fp64.
The exception is swallowed and word timestamps silently degrade to an
even split, so the logits are cast with `.float()` before alignment.
