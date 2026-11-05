"""Read-only WinMM device inspection; a matching pair does not prove a cable."""


def inspect(port: str) -> dict:
    try:
        from app.hosting.midi import list_inputs
        from app.hosting.midi_output import list_outputs
        inputs = [item["name"] for item in list_inputs(strict=True)]
        outputs = list_outputs(strict=True)
    except Exception:  # WinMM may be absent or a device may disappear during enumeration.
        return {"port": port, "state": "unavailable", "input_matches": None,
                "output_matches": None, "outputs": [],
                "message": "Could not list MIDI devices. Refresh after checking Windows MIDI devices."}
    ins, outs = inputs.count(port), outputs.count(port)
    if outs > 1:
        state, message = "ambiguous", "More than one output has this name. Select a uniquely named output."
    elif not outs:
        state, message = "missing", "The selected output is missing. Create or connect a MIDI port, then refresh."
    else:
        state, message = "available", "Output is listed. It may still be busy; this does not confirm a connection."
    return {"port": port, "state": state, "input_matches": ins, "output_matches": outs,
            "outputs": outputs, "message": message}
