"""Synthetic SRT transcripts for the replay tests, written to a temporary folder at test time.

Fictional conversations with the CEO of Brioche & Four (the example client), on two streams like the
live capture: `fr` (fr-FR recogniser, readable) and `en` (en-US recogniser: phonetic gibberish while
French is spoken, readable during the one English passage).

- `clean`: an interviewer and the CEO, 27 cues per stream.
- `noisy`: vague answers, a TV in the room, pauses, en-US gibberish, 20 cues per stream.
"""

from __future__ import annotations

from pathlib import Path

CLEAN_FR = [
    ('00:00:01,200', '00:00:06,000', 'Bonjour, merci de prendre ce temps. Je suis le 5D qui va travailler avec vos équipes.'),
    ('00:00:06,400', '00:00:11,800', 'Avec plaisir. Chez brioche et four, on distribue les fours for naxa depuis huit ans.'),
    ('00:00:12,500', '00:00:18,900', "Pour commencer, dans un an, qu'est-ce qui vous ferait dire que ce projet a réussi ?"),
    ('00:00:19,600', '00:00:27,400', "Que mes équipes arrêtent la ressaisie. Aujourd'hui on perd un temps fou, tout est dans Excel."),
    ('00:00:27,900', '00:00:33,500', 'On ressaisit les commandes des boulangeries à la main, une par une.'),
    ('00:00:34,100', '00:00:41,000', 'On est dix-huit salariés, à peu près six cents boulangeries clientes et neuf cents machines par an.'),
    ('00:00:41,800', '00:00:47,200', "Comment se passe une commande à l'usine for naxa, de la prévision jusqu'à l'arrivée ?"),
    ('00:00:47,900', '00:00:56,300', "On fait une prévision par saison, on commande à l'usine en Italie, et il faut compter dix à douze semaines."),
    ('00:00:56,800', '00:01:02,400', "Entre la commande et l'arrivée du camion à notre entrepôt. Le transporteur gère les documents."),
    ('00:01:03,000', '00:01:07,500', "Et le stock, vous avez une vision fiable aujourd'hui ?"),
    ('00:01:08,100', '00:01:16,600', "Franchement non. Le stock de fours est à peu près à jour, mais les pièces c'est une catastrophe, l'inventaire ne colle jamais."),
    ('00:01:17,200', '00:01:25,900', "Depuis la grosse panne de serveur l'année dernière, on a revu les sauvegardes avec le prestataire."),
    ('00:01:26,400', '00:01:32,000', "On a perdu deux semaines de données et on a reconstruit l'heure pé en urgence."),
    ('00:01:32,800', '00:01:39,400', 'Laisse mi souiche tout inglishe pour une seconde. Ouate retourne on investe ment dou you expecte ?'),
    ('00:01:40,000', '00:01:47,600', 'Honnestli oui nid paie bac ouise in twelve monthes. If oui save tou pipeuls time on ordeur entri.'),
    ('00:01:48,000', '00:01:51,900', "Dat's olrédi worse it."),
    ('00:01:52,600', '00:01:56,800', 'And bajet for ze first sixe monthes ?'),
    ('00:01:57,300', '00:02:03,000', 'Araound fifti tou eïti sousande euros, notte more.'),
    ('00:02:04,000', '00:02:10,200', "Merci. Côté boulangeries, comment est-ce qu'elles passent commande aujourd'hui ?"),
    ('00:02:10,900', '00:02:18,800', "Par mail et par téléphone surtout. On a un vieux portail mais personne ne l'utilise."),
    ('00:02:19,400', '00:02:25,600', 'Et elles nous appellent dix fois par jour pour savoir où en est leur commande.'),
    ('00:02:26,300', '00:02:30,400', "D'accord. Et pour la garantie, comment ça se passe ?"),
    ('00:02:31,000', '00:02:39,500', 'La boulangerie envoie les photos du four par mail, on monte le dossier dans le portail for naxa,'),
    ('00:02:40,000', '00:02:45,300', 'et le remboursement prend parfois trois mois. Ça bloque de la trésorerie.'),
    ('00:02:46,000', '00:02:51,400', 'Dernière chose : qui doit être impliqué dans la décision ?'),
    ('00:02:52,000', '00:02:58,600', 'Moi, notre directeur financier, et le prestataire informatique pour la sécurité.'),
    ('00:02:59,300', '00:03:03,000', "Très bien. C'est parfait, merci beaucoup."),
]

CLEAN_EN = [
    ('00:00:01,550', '00:00:06,350', 'Bone jury mercy the prawn de set ton. Jesus le 5D key va travail ever vows a key.'),
    ('00:00:06,750', '00:00:12,150', 'Ava plays here. Shea bree osh a four, on this tree bue lay four for Naxa the PUI wheat on.'),
    ('00:00:12,850', '00:00:19,250', "Pure commence say done's on case key vu fair deer ke sir project a racy."),
    ('00:00:19,950', '00:00:27,750', "Commie a key pirate on la rest easy. O'Jordi on pair on tom foo, two today don't Excel."),
    ('00:00:28,250', '00:00:33,850', "On race easy lake o'man de boo lawn jerry a la man, Hoon pair Hoon."),
    ('00:00:34,450', '00:00:41,350', 'On a dees wheat salary, a pepper see sauna boo lawn jerry and nerf son machine par on.'),
    ('00:00:42,150', '00:00:47,550', 'Come on supper soon come on the loose in for Naxa della prevision you ska la reveille.'),
    ('00:00:48,250', '00:00:56,650', 'On fet Hoon prevision pass his son, on come on the loose in on Italy, a fake on taste he a dues similar.'),
    ('00:00:57,150', '00:01:02,750', 'On trail a come on day la reveille do Camille a not lawn trip oh. Le transport ER jair lay document.'),
    ('00:01:03,350', '00:01:07,850', "A la stock, vous ave Hoon vision fee arble o'Jordi."),
    ('00:01:08,450', '00:01:16,950', 'Fresh mon non. Les stock de four a pepper a jour, may lay piece set Hoon catastrophe, lent on tear knee coal jam a.'),
    ('00:01:17,550', '00:01:26,250', "The pre-la gross pan de server l'any their near, on a revue lay so very guard of they press tat tear."),
    ('00:01:26,750', '00:01:32,350', "On a pair do this many de Donna a on a recount true i'll airport on urgent."),
    ('00:01:33,150', '00:01:39,750', 'Let me switch to English for a second. What return on investment do you expect?'),
    ('00:01:40,350', '00:01:47,950', "Honestly, we need payback within twelve months. If we save two people's time on order entry,"),
    ('00:01:48,350', '00:01:52,250', "that's already worth it."),
    ('00:01:52,950', '00:01:57,150', 'And budget for the first six months?'),
    ('00:01:57,650', '00:02:03,350', 'Around fifty to eighty thousand euros, not more.'),
    ('00:02:04,350', '00:02:10,550', "Mercy. Coat a boo lawn jerry, come on s'kill pass come on the o'Jordi."),
    ('00:02:11,250', '00:02:19,150', 'Pa male a party left on sir too. On a and view portal may pair sun ne lieut tea lease.'),
    ('00:02:19,750', '00:02:25,950', 'Ail nu za pel this foie pa jour poor so where you on a leur come on.'),
    ('00:02:26,650', '00:02:30,750', 'Dakar. A poor la guarantee, come on Sasa pass.'),
    ('00:02:31,350', '00:02:39,850', 'La boo lawn jerry on Voila photo do four pa male, on mount le dossier don le portal for Naxa,'),
    ('00:02:40,350', '00:02:45,650', 'a le rum boor small prawn pa foie trois mois. Sa block de la tray so wreath.'),
    ('00:02:46,350', '00:02:51,750', 'Their near shows key dwa et rem please kay don la day see zion.'),
    ('00:02:52,350', '00:02:58,950', 'Mwah, not director fee non see a, a le press tat tear Anne for Matic poor la secure tea.'),
    ('00:02:59,650', '00:03:03,350', 'Tray be on. Say par fey, mercy bow coo.'),
]

NOISY_FR = [
    ('00:00:01,000', '00:00:06,500', "Dans un an, qu'est-ce qui aurait changé concrètement pour que vous disiez que ça a marché ?"),
    ('00:00:07,500', '00:00:12,000', "Ben que ça aille plus vite, qu'on soit plus efficaces, voilà."),
    ('00:00:12,600', '00:00:18,000', "D'accord. Concrètement, plus vite sur quoi, et comment vous le verriez ?"),
    ('00:00:18,800', '00:00:27,500', "Les commandes des boulangeries. Aujourd'hui une commande met deux jours à partir, je voudrais que ce soit le jour même."),
    ('00:00:28,000', '00:00:35,000', "Et qu'Inès arrête de passer ses matinées à ressaisir, d'ici six mois disons."),
    ('00:00:41,000', '00:00:47,000', 'Si vous pensez à la semaine dernière, quelle tâche a pris le plus de temps à vos équipes pour rien ?'),
    ('00:00:47,800', '00:00:53,000', "Ah c'est les commandes. Les commandes arrivent par mail chez Inès, voilà."),
    ('00:00:53,400', '00:00:57,800', "Et la météo annonce encore de la pluie sur toute la Bretagne ce week-end, on n'avait jamais vu ça."),
    ('00:00:58,500', '00:01:03,500', "Et on a un ERP, mais bon. On sait à peu près ce qu'il y a dans l'entrepôt."),
    ('00:01:04,000', '00:01:10,500', "Prenons la dernière commande d'Inès : elle l'a saisie dans quel outil, et ça lui a pris combien de temps ?"),
    ('00:01:11,200', '00:01:20,500', 'Dans Sage. Lundi elle a eu la commande du Fournil du Parc à Lyon, douze lignes, elle a mis quarante minutes.'),
    ('00:01:21,000', '00:01:28,000', 'Et elle en a une trentaine par semaine comme ça, donc une bonne partie de ses matinées.'),
    ('00:01:34,000', '00:01:40,000', 'Pour situer : combien de personnes chez vous, combien de boulangeries clientes actives, combien de machines par an ?'),
    ('00:01:40,800', '00:01:48,500', "On est dix-huit, à peu près six cents boulangeries actives, et neuf cents machines l'an dernier."),
    ('00:01:55,000', '00:02:01,500', "Racontez-moi la dernière commande d'une boulangerie, depuis le mail ou l'appel jusqu'à la livraison."),
    ('00:02:02,300', '00:02:07,000', 'Bon, ça arrive par mail, Inès la saisit, et après ça part.'),
    ('00:02:07,600', '00:02:13,000', 'Let me switch to English for a second: after she types it in Sage, who prepares the shipment?'),
    ('00:02:13,800', '00:02:22,000', 'Laisse mi cie, ze warehouse guy, Hugo, he prints the picking list from Sage, and it ships in two days.'),
    ('00:02:22,600', '00:02:28,000', "Et ça arrive combien de fois qu'il manque une pièce au moment de préparer ?"),
    ('00:02:28,800', '00:02:36,000', "Une fois sur cinq à peu près, parce que le stock dans Sage n'est pas à jour."),
]

NOISY_EN = [
    ('00:00:01,300', '00:00:06,800', 'Dance on on, kiss key or yeah shown ji concrete mom pour que vous dizzy key sa a marché.'),
    ('00:00:07,800', '00:00:12,300', 'Ben kiss I plus feet, con so plus efficacy, while ah.'),
    ('00:00:12,900', '00:00:18,300', 'Dakar. Concrete mom, plus feet sir kwa, a common vu le very yay.'),
    ('00:00:19,100', '00:00:27,800', "Lake o'man de boo lawn jerry. O'Jordi tornay come on the met do jaw a party, avic so more."),
    ('00:00:28,300', '00:00:35,300', 'A kiness a rest de passer said Matt in a rest easy, disease six mwah dizzy on.'),
    ('00:00:41,300', '00:00:47,300', 'See vu pansy a la semen their near, kel tash a pre le plus de tom a vows a key pour rien.'),
    ('00:00:48,100', '00:00:53,300', "Ah say lake o'man. Lake o'man the rave par male shea in ness, while ah."),
    ('00:00:53,700', '00:00:58,100', 'A la may tay oh a non sank or de la plu sir tout la bread tan ye, on navy jam a view sa.'),
    ('00:00:58,800', '00:01:03,800', 'A on a an ERP, may bone. On say a pepper skill ya dawn lawn trip oh.'),
    ('00:01:04,300', '00:01:10,800', 'Pray none la their near come on de in ness, L la say see dawn kel ooh tea, a sa lui a pre come be ah de tom.'),
    ('00:01:11,500', '00:01:20,800', "Dawn Sage. Lundy el a you la come on du four nil du park a Lyon, do's lean, el a me karate minute."),
    ('00:01:21,300', '00:01:28,300', 'A el on a une tron ten par semen come sa, donk une bone party de say Matt in a.'),
    ('00:01:34,300', '00:01:40,300', 'Pour see two A, come be on de pear son shea vu, come be on de boo lawn jerry active.'),
    ('00:01:41,100', '00:01:48,800', 'On a dees wheat, a pepper see sauna boo lawn jerry active, a nerf son machine lawn their near.'),
    ('00:01:55,300', '00:02:01,800', 'Rack on tame wa la their near come on dune boo lawn jerry, the PUI le male oo lap pell.'),
    ('00:02:02,600', '00:02:07,300', 'Bone, sa a reef par male, in ness la say see, a a pray sa par.'),
    ('00:02:07,900', '00:02:13,300', 'Let me switch to English for a second, after she types it in Sage, who prepares the shipment?'),
    ('00:02:14,100', '00:02:22,300', 'Let me see, the warehouse guy, Hugo, he prints the picking list from Sage, and it ships in two days.'),
    ('00:02:22,900', '00:02:28,300', 'A sa a reef come be on de foie kill mon cune piece oh mom on de pray par ray.'),
    ('00:02:29,100', '00:02:36,300', 'Une foie sir sank a pepper, par sky le stock dawn Sage nest pa a jaw.'),
]

SETS = {"clean": {"fr": CLEAN_FR, "en": CLEAN_EN}, "noisy": {"fr": NOISY_FR, "en": NOISY_EN}}


def srt_text(cues: list[tuple[str, str, str]]) -> str:
    return "\n".join(f"{i}\n{start} --> {end}\n{text}\n" for i, (start, end, text) in enumerate(cues, 1))


def write_samples(root: Path) -> Path:
    """Write every set as root/<set>/<stream>.srt and return root."""
    for name, streams in SETS.items():
        (root / name).mkdir(parents=True, exist_ok=True)
        for stream, cues in streams.items():
            (root / name / f"{stream}.srt").write_text(srt_text(cues), encoding="utf-8")
    return root
