"""Rolling merged transcript built from the FR and EN recogniser streams."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from .srt import format_clock


@dataclass
class Segment:
    seq: int  # insertion order, used for "what's new since last sync"
    stream: str  # "fr" | "en"
    start: float  # seconds since session start
    end: float
    text: str
    received_at: float = 0.0  # wall clock (epoch) when the app ingested the cue


class Transcript:
    def __init__(self) -> None:
        self.segments: list[Segment] = []
        self._seq = 0

    @property
    def last_seq(self) -> int:
        return self._seq

    def add(self, stream: str, start: float, end: float, text: str, received_at: float = 0.0) -> Segment:
        self._seq += 1
        seg = Segment(self._seq, stream, float(start), float(end), text.strip(), float(received_at))
        self.segments.append(seg)
        return seg

    def since(self, seq: int) -> list[Segment]:
        """Segments added after `seq`, in time order (the merged FR+EN view)."""
        return sorted((s for s in self.segments if s.seq > seq), key=_time_key)

    def context_before(self, seq: int, seconds: float) -> list[Segment]:
        """Already-synced segments from the last `seconds` before the delta, for continuity."""
        old = sorted((s for s in self.segments if s.seq <= seq), key=_time_key)
        if not old:
            return []
        horizon = old[-1].end - seconds
        return [s for s in old if s.end >= horizon]

    def merged(self) -> list[Segment]:
        return sorted(self.segments, key=_time_key)

    def tail(self, n: int, stream: str | None = None) -> list[Segment]:
        segs = [s for s in self.merged() if stream is None or s.stream == stream]
        return segs[-n:]

    def readable(self, segments: list[Segment] | None = None, primary: str = "fr") -> list[Segment]:
        """Keep, for each moment, the stream that makes sense.

        Each recogniser outputs gibberish for the other language. For every cue we look at
        the cues of the other stream that overlap it in time and keep it only if it reads
        better in its own language (share of function words of its language minus those of the
        other language), with a small bias for the session language. A cue with no overlapping counterpart (e.g. the other
        recogniser died) is always kept. Display only: Claude does the real selection.
        """
        segs = self.merged() if segments is None else sorted(segments, key=_time_key)
        langs = {stream_language(s.stream) for s in segs}

        def score(seg: Segment) -> float:
            # distinctive: function words of its own language minus those of the other one(s)
            # ("on", "a", "de" exist in both, so gibberish built from them scores ~0)
            mine = stream_language(seg.stream)
            others = [lang for lang in langs if lang != mine] or (["en"] if mine == "fr" else ["fr"])
            own = readability(seg.text, mine) - max(readability(seg.text, o) for o in others)
            return own + (PRIMARY_BIAS if stream_language(seg.stream) == stream_language(primary) else 0.0)

        out = []
        for s in segs:
            rivals = [o for o in segs if o.stream != s.stream and o.start < s.end and s.start < o.end]
            if not rivals:
                out.append(s)
                continue
            best = max(score(o) for o in rivals)
            mine = score(s)
            if mine > best or (mine == best and stream_language(s.stream) == stream_language(primary)):
                out.append(s)
        return out

    def readable_tail(self, n: int, primary: str = "fr") -> list[Segment]:
        recent = self.merged()[-4 * n :]
        return self.readable(recent, primary)[-n:]

    # --- persistence ------------------------------------------------------
    def save_jsonl(self, path: Path) -> None:
        tmp = path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            for s in self.segments:
                fh.write(json.dumps(asdict(s), ensure_ascii=False) + "\n")
        tmp.replace(path)

    @classmethod
    def load_jsonl(cls, path: Path) -> "Transcript":
        t = cls()
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    d = json.loads(line)
                    seg = Segment(**d)
                    t.segments.append(seg)
                    t._seq = max(t._seq, seg.seq)
        return t


_WORDS = {
    "fr": set(
        "le la les un une des de du d l et est sont à au aux en dans pour par sur avec sans que qui quoi "
        "ce cette ces c ça on nous vous je j il ils elle elles ne n pas plus mais ou donc car se s son sa "
        "ses leur leurs mon ma mes notre nos votre vos y a ont été être avoir fait faire très bien aussi "
        "comme tout tous quand où alors oui non merci bonjour ai chez depuis".split()
    ),
    "en": set(
        "the a an and is are was were be been to of in on at for with without that which who what this "
        "these those it its we you they he she i me my our your their not no but or so if then there "
        "have has had do does did can will would should could about from by as just very also all when "
        "where yes thanks hello s need".split()
    ),
    "de": set(
        "der die das den dem des ein eine einen einem und ist sind war waren zu von mit für auf nicht "
        "ich wir sie er es ihr du man auch aber oder wenn dann noch schon sehr wie was wer wo hat haben "
        "wird werden kann können bei nach aus im ins zum zur über ja nein danke also mal".split()
    ),
    "es": set(
        "el la los las un una unos unas y es son era fue de del al en por para con sin que qué quien "
        "como cuando donde no sí pero o se lo le les su sus mi nuestro nosotros ustedes usted yo muy "
        "también hay tiene tenemos está están gracias hola bueno".split()
    ),
    "it": set(
        "il lo la i gli le un una uno e è sono era di del della dei in per con su che chi non sì ma o "
        "se si ci mi ti lo suo sua nostro noi voi io molto anche come quando dove ha hanno abbiamo "
        "grazie buongiorno allora".split()
    ),
    "pt": set(
        "o a os as um uma e é são era de do da dos das em no na por para com sem que quem não sim mas "
        "ou se seu sua nosso nós você eu muito também como quando onde tem temos está obrigado".split()
    ),
    "nl": set(
        "de het een en is zijn was waren van met voor op niet ik wij we jullie hij zij ze dat die dit "
        "wat wie waar hoe ook maar of als dan nog heel heeft hebben kan kunnen bij naar uit ja nee "
        "dank".split()
    ),
}
PRIMARY_BIAS = 0.1
_TOKEN = re.compile(r"[a-zàâäçéèêëîïôöùûüÿœæßñáíóúãõì]+", re.I)


def stream_language(stream: str) -> str:
    """Stream id -> language: 'fr' -> 'fr', 'en-us' -> 'en'."""
    return stream.split("-")[0].lower()


def readability(text: str, lang: str) -> float:
    """Share of tokens that are common function words of `lang` (0..1)."""
    tokens = [t.lower() for t in _TOKEN.findall(text)]
    if not tokens:
        return 0.0
    vocab = _WORDS.get(lang, set())
    return sum(1 for t in tokens if t in vocab) / len(tokens)


def _time_key(s: Segment) -> tuple[float, str, int]:
    return (s.start, s.stream, s.seq)


def render_lines(segments: list[Segment], ids: bool = False) -> str:
    """Prompt/plan format: one line per recognised cue, tagged with time and stream (and the
    line id when the model must refer to individual lines)."""
    if ids:
        return "\n".join(f"[{format_clock(s.start)} #{s.seq}] {s.stream.upper()}: {s.text}" for s in segments)
    return "\n".join(f"[{format_clock(s.start)}] {s.stream.upper()}: {s.text}" for s in segments)
