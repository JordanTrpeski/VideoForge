# Background loop clips

Drop royalty-free vertical (or any-aspect) video clips here — e.g. Minecraft
parkour, subway surfers, satisfying-loop footage. When
`pipeline.visual_mode` is set to `background_loop` in `config.json`, the
assembly engine picks one of these at random, takes a random start offset,
loops or trims it to match the voiceover length, and scale-crops it to the
configured portrait resolution (1080x1920 by default).

Supported extensions: `.mp4`, `.mov`, `.mkv`, `.webm`

Use only clips you have the rights to use. No copyrighted gameplay/music.
This folder is the visual source for Reddit Stories and any other clip-based
content mode — zero image-generation (Leonardo.AI) calls are made in this mode.
