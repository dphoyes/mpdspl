#!/usr/bin/env python3

import argparse
import yaml
import pathlib
import collections
import functools
import typing
import mpd


def execfile(filename, globals):
    with open(filename, "rb") as fin:
         source = fin.read()
    code = compile(source, filename, "exec")
    exec(code, globals)


class DbAccessor:
    _mpd: mpd.MPDClient
    _map_path_to_idx: typing.Dict[pathlib.Path, int]
    _cache: dict

    def __init__(self, m, map_path_to_idx):
        self._mpd = m
        self._map_path_to_idx = map_path_to_idx
        self._cache = {}

    def _lookup(self, arg):
        raise LookupError

    def __getitem__(self, arg):
        try:
            return self._cache[arg]
        except KeyError:
            pass
        try:
            ret = self._cache[arg] = self._lookup(arg)
            return ret
        except LookupError as e:
            raise IndexError(*e.args)

    def __getattr__(self, arg):
        try:
            return self.__getitem__(arg)
        except LookupError as e:
            raise AttributeError(*e.args)


class PlaylistAccessor(DbAccessor):
    def __init__(self, *args, generated_playlists, **kwargs):
        super().__init__(*args, **kwargs)
        self.__generated_playlists = generated_playlists
        self.__frozen_keys = set()

    def _lookup(self, arg):
        try:
            raw_result = self._mpd.listplaylist(arg)
        except mpd.CommandError:
            raise LookupError(f"No such playlist \"{arg}\"")
        p2i = self._map_path_to_idx
        result = [p2i.get(pathlib.Path(x)) for x in raw_result]
        return tuple([x for x in result if x is not None])

    def __getitem__(self, arg):
        try:
            return super().__getitem__(arg)
        finally:
            self.__frozen_keys.add(arg)

    def __setitem__(self, key, value):
        if key in self.__frozen_keys:
            raise KeyError(f"Cannot modify playlist {key} after its contents have already been queried for something else")
        self.__generated_playlists[key] = self._cache[key] = value


class GenreAccessor(DbAccessor):
    def _lookup(self, arg):
        raw_result = self._mpd.find(f"(Genre == '{arg}')")
        p2i = self._map_path_to_idx
        result = [p2i.get(pathlib.Path(x['file'])) for x in raw_result]
        return frozenset([x for x in result if x is not None])


class LabelAccessor(DbAccessor):
    @functools.cached_property
    def __all_tracks_by_path(self):
        result = collections.defaultdict(list)
        for p, i in self._map_path_to_idx.items():
            result[p].append(i)
            for parent in p.parents:
                result[parent].append(i)
        return {p: frozenset(l) for p, l in result.items()}

    def _lookup(self, arg):
        result = set()
        all_tracks_by_path = self.__all_tracks_by_path
        music_root = pathlib.Path(self._mpd.config())
        label_found = False
        yml_file_name = f'.label.{arg}.yml'
        for tag_file in music_root.rglob(yml_file_name):
            with open(tag_file) as f:
                tag_file_content = yaml.safe_load(f)
            label_found = True
            tag_file_dir = tag_file.relative_to(music_root).parent
            key, given_list = tag_file_content.popitem()
            assert len(tag_file_content) == 0
            if key == "all_except":
                result |= all_tracks_by_path[tag_file_dir]
                if given_list is not None:
                    for p in given_list:
                        result -= all_tracks_by_path[tag_file_dir / p]
            elif key == "none_except":
                result -= all_tracks_by_path[tag_file_dir]
                if given_list is not None:
                    for p in given_list:
                        result |= all_tracks_by_path[tag_file_dir / p]
        if not label_found:
            raise LookupError(f"No file found named {yml_file_name}")
        return frozenset(result)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mpd")
    parser.add_argument("exec_file", type=pathlib.Path)
    return parser.parse_args()


def generate_all(exec_file: pathlib.Path, m: mpd.MPDClient):
    all_track_paths = sorted(pathlib.Path(obj['file']) for obj in m.listall() if 'file' in obj)
    map_path_to_idx = {p: i for i, p in enumerate(all_track_paths)}

    generated_playlists = dict()

    execfile(exec_file, {
        "all_tracks": frozenset(range(len(all_track_paths))),
        "genre": GenreAccessor(m, map_path_to_idx),
        "label": LabelAccessor(m, map_path_to_idx),
        "playlist": PlaylistAccessor(m, map_path_to_idx, generated_playlists=generated_playlists),
    })

    for name, track_idxs in generated_playlists.items():
        if isinstance(track_idxs, (set, frozenset)):
            track_idxs = sorted(track_idxs)
        track_names = [str(all_track_paths[i]) for i in track_idxs]

        try:
            old_track_names = m.listplaylist(name)
        except mpd.CommandError:
            old_track_names = []
            already_exists = False
        else:
            already_exists = True

        if not already_exists and not track_names:
            print(f"Sending empty {name} to MPD")
            m.command_list_ok_begin()
            m.save(name)
            m.playlistclear(name)
            m.command_list_end()
        elif track_names != old_track_names:
            print(f"Sending {name} to MPD")
            m.command_list_ok_begin()
            if already_exists:
                m.playlistclear(name)
            for x in track_names:
                m.playlistadd(name, x)
            m.command_list_end()
        else:
            print(f"No change for {name}")


def main():
    args = parse_args()
    m = mpd.MPDClient()
    m.connect(args.mpd)
    m.tagtypes("clear")

    while True:
        generate_all(args.exec_file, m)
        while True:
            m.idle("update")
            print("MPD update triggered")
            if m.status().get("updating_db", -1) == -1:
                break


if __name__ == "__main__":
    main()
