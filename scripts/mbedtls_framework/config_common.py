"""Mbed TLS and PSA configuration file manipulation library
"""

## Copyright The Mbed TLS Contributors
## SPDX-License-Identifier: Apache-2.0 OR GPL-2.0-or-later
##

import argparse
import os
import re
import sys

from abc import ABCMeta


class Setting:
    """Representation of one Mbed TLS mbedtls_config.h pr PSA crypto_config.h setting.

    Fields:
    * name: the symbol name ('MBEDTLS_xxx').
    * value: the value of the macro. The empty string for a plain #define
      with no value.
    * active: True if name is defined, False if a #define for name is
      present in mbedtls_config.h but commented out.
    * section: the name of the section that contains this symbol.
    * configfile: the file the settings is defined
    """
    # pylint: disable=too-few-public-methods, too-many-arguments
    def __init__(self, configfile, active, name, value='', section=None):
        self.active = active
        self.name = name
        self.value = value
        self.section = section
        self.configfile = configfile


class Config:
    """Representation of the Mbed TLS and PSA configuration.

    In the documentation of this class, a symbol is said to be *active*
    if there is a #define for it that is not commented out, and *known*
    if there is a #define for it whether commented out or not.

    This class supports the following protocols:
    * `name in config` is `True` if the symbol `name` is active, `False`
      otherwise (whether `name` is inactive or not known).
    * `config[name]` is the value of the macro `name`. If `name` is inactive,
      raise `KeyError` (even if `name` is known).
    * `config[name] = value` sets the value associated to `name`. `name`
      must be known, but does not need to be set. This does not cause
      name to become set.
    """

    def __init__(self):
        self.settings = {}
        self.configfiles = []

    def __contains__(self, name):
        """True if the given symbol is active (i.e. set).

        False if the given symbol is not set, even if a definition
        is present but commented out.
        """
        return name in self.settings and self.settings[name].active

    def all(self, *names):
        """True if all the elements of names are active (i.e. set)."""
        return all(name in self for name in names)

    def any(self, *names):
        """True if at least one symbol in names are active (i.e. set)."""
        return any(name in self for name in names)

    def known(self, name):
        """True if a #define for name is present, whether it's commented out or not."""
        return name in self.settings

    def __getitem__(self, name):
        """Get the value of name, i.e. what the preprocessor symbol expands to.

        If name is not known, raise KeyError. name does not need to be active.
        """
        return self.settings[name].value

    def get(self, name, default=None):
        """Get the value of name. If name is inactive (not set), return default.

        If a #define for name is present and not commented out, return
        its expansion, even if this is the empty string.

        If a #define for name is present but commented out, return default.
        """
        if name in self.settings:
            return self.settings[name].value
        else:
            return default

    def __setitem__(self, name, value):
        """If name is known, set its value.

        If name is not known, raise KeyError.
        """
        setting = self.settings[name]
        if setting != value:
            setting.configfile.modified = True

        setting.value = value

    def set(self, name, value=None):
        """Set name to the given value and make it active.

        If value is None and name is already known, don't change its value.
        If value is None and name is not known, set its value.
        """
        if name in self.settings:
            setting = self.settings[name]
            if setting.value != value or not setting.active:
                setting.configfile.modified = True
            if value is not None:
                setting.value = value
            setting.active = True
        else:
            configfile = self._get_configfile(name)
            self.settings[name] = Setting(configfile, True, name, value=value)
            configfile.modified = True

    def unset(self, name):
        """Make name unset (inactive).

        name remains known if it was known before.
        """
        if name not in self.settings:
            return

        setting = self.settings[name]
        # Check if modifying the config file
        if setting.active:
            setting.configfile.modified = True

        setting.active = False

    def adapt(self, adapter):
        """Run adapter on each known symbol and (de)activate it accordingly.

        `adapter` must be a function that returns a boolean. It is called as
        `adapter(name, active, section)` for each setting, where `active` is
        `True` if `name` is set and `False` if `name` is known but unset,
        and `section` is the name of the section containing `name`. If
        `adapter` returns `True`, then set `name` (i.e. make it active),
        otherwise unset `name` (i.e. make it known but inactive).
        """
        for setting in self.settings.values():
            is_active = setting.active
            setting.active = adapter(setting.name, setting.active,
                                     setting.section)
            # Check if modifying the config file
            if setting.active != is_active:
                setting.configfile.modified = True

    def change_matching(self, regexs, enable):
        """Change all symbols matching one of the regexs to the desired state."""
        if not regexs:
            return
        regex = re.compile('|'.join(regexs))
        for setting in self.settings.values():
            if regex.search(setting.name):
                # Check if modifying the config file
                if setting.active != enable:
                    setting.configfile.modified = True
                setting.active = enable

    def _get_configfile(self, name=None):
        """Find a config for a setting name.

        If more then one configfile is used this function must be overridden.
        """

        if name and name in self.settings:
            return self.get(name).configfile
        return self.configfiles[0]

    def write(self, filename=None):
        """Write the whole configuration to the file it was read from.

        If filename is specified, write to this file instead.
        """

        for configfile in self.configfiles:
            configfile.write(self.settings, filename)

    def filename(self, name=None):
        """Get the name of the config file."""

        return self._get_configfile(name).filename


class ConfigFile(metaclass=ABCMeta):
    """Representation of a configuration file."""

    def __init__(self, default_path, name, filename=None):
        """Check if the config file exists."""
        if filename is None:
            for candidate in default_path:
                if os.path.lexists(candidate):
                    filename = candidate
                    break
            else:
                raise FileNotFoundError(f'{name} configuration file not found: '
                                        f'{filename if filename else default_path}')

        self.filename = filename
        self.templates = []
        self.current_section = None
        self.inclusion_guard = None
        self.modified = False

    _define_line_regexp = (r'(?P<indentation>\s*)' +
                           r'(?P<commented_out>(//\s*)?)' +
                           r'(?P<define>#\s*define\s+)' +
                           r'(?P<name>\w+)' +
                           r'(?P<arguments>(?:\((?:\w|\s|,)*\))?)' +
                           r'(?P<separator>\s*)' +
                           r'(?P<value>.*)')
    _ifndef_line_regexp = r'#ifndef (?P<inclusion_guard>\w+)'
    _section_line_regexp = (r'\s*/?\*+\s*[\\@]name\s+SECTION:\s*' +
                            r'(?P<section>.*)[ */]*')
    _config_line_regexp = re.compile(r'|'.join([_define_line_regexp,
                                                _ifndef_line_regexp,
                                                _section_line_regexp]))
    def _parse_line(self, line):
        """Parse a line in the config file, save the templates representing the lines
           and return the corresponding setting element.
        """

        line = line.rstrip('\r\n')
        m = re.match(self._config_line_regexp, line)
        if m is None:
            self.templates.append(line)
            return None
        elif m.group('section'):
            self.current_section = m.group('section')
            self.templates.append(line)
            return None
        elif m.group('inclusion_guard') and self.inclusion_guard is None:
            self.inclusion_guard = m.group('inclusion_guard')
            self.templates.append(line)
            return None
        else:
            active = not m.group('commented_out')
            name = m.group('name')
            value = m.group('value')
            if name == self.inclusion_guard and value == '':
                # The file double-inclusion guard is not an option.
                self.templates.append(line)
                return None
            template = (name,
                        m.group('indentation'),
                        m.group('define') + name +
                        m.group('arguments') + m.group('separator'))
            self.templates.append(template)

            return (active, name, value, self.current_section)

    def parse_file(self):
        """Parse the whole file and return the settings."""

        with open(self.filename, 'r', encoding='utf-8') as file:
            for line in file:
                setting = self._parse_line(line)
                if setting is not None:
                    yield setting
        self.current_section = None

    #pylint: disable=no-self-use
    def _format_template(self, setting, indent, middle):
        """Build a line for the config file for the given setting.

        The line has the form "<indent>#define <name> <value>"
        where <middle> is "#define <name> ".
        """

        value = setting.value
        if value is None:
            value = ''
        # Normally the whitespace to separate the symbol name from the
        # value is part of middle, and there's no whitespace for a symbol
        # with no value. But if a symbol has been changed from having a
        # value to not having one, the whitespace is wrong, so fix it.
        if value:
            if middle[-1] not in '\t ':
                middle += ' '
        else:
            middle = middle.rstrip()
        return ''.join([indent,
                        '' if setting.active else '//',
                        middle,
                        value]).rstrip()

    def write_to_stream(self, settings, output):
        """Write the whole configuration to output."""

        for template in self.templates:
            if isinstance(template, str):
                line = template
            else:
                name, indent, middle = template
                line = self._format_template(settings[name], indent, middle)
            output.write(line + '\n')

    def write(self, settings, filename=None):
        """Write the whole configuration to the file it was read from.

        If filename is specified, write to this file instead.
        """

        if filename is None:
            filename = self.filename

        # Not modified so no need to write to the file
        if not self.modified and filename == self.filename:
            return

        with open(filename, 'w', encoding='utf-8') as output:
            self.write_to_stream(settings, output)


class ConfigTool(metaclass=ABCMeta):
    """Command line config manipulation tool.

    Custom parser option can be added by overriding 'custom_parser_options'.
    """

    def __init__(self, file_type):
        """Create parser for config manipulation tool."""

        self.parser = argparse.ArgumentParser(description="""
                                              Configuration file manipulation tool.""")
        self.subparsers = self.parser.add_subparsers(dest='command',
                                                     title='Commands')
        self._common_parser_options(file_type)
        self.custom_parser_options()
        self.parser_args = self.parser.parse_args()
        self.config = Config() # Make the pylint happy

    def add_adapter(self, name, function, description):
        """Creates a command in the tool for a configuration adapter."""

        subparser = self.subparsers.add_parser(name, help=description)
        subparser.set_defaults(adapter=function)

    def _common_parser_options(self, file_type):
        """Common parser options for config manipulation tool."""

        self.parser.add_argument(
            '--file', '-f',
            help="""File to read (and modify if requested). Default: {}.
                 """.format(file_type.default_path))
        self.parser.add_argument(
            '--force', '-o',
            action='store_true',
            help="""For the set command, if SYMBOL is not present, add a definition for it.""")
        self.parser.add_argument(
            '--write', '-w',
            metavar='FILE',
            help="""File to write to instead of the input file.""")

        parser_get = self.subparsers.add_parser(
            'get',
            help="""Find the value of SYMBOL and print it. Exit with
                 status 0 if a #define for SYMBOL is found, 1 otherwise.""")
        parser_get.add_argument('symbol', metavar='SYMBOL')
        parser_set = self.subparsers.add_parser(
            'set',
            help="""Set SYMBOL to VALUE. If VALUE is omitted, just uncomment
                 the #define for SYMBOL. Error out of a line defining
                 SYMBOL (commented or not) is not found, unless --force is passed. """)
        parser_set.add_argument('symbol', metavar='SYMBOL')
        parser_set.add_argument('value', metavar='VALUE', nargs='?', default='')
        parser_set_all = self.subparsers.add_parser(
            'set-all',
            help="""Uncomment all #define whose name contains a match for REGEX.""")
        parser_set_all.add_argument('regexs', metavar='REGEX', nargs='*')
        parser_unset = self.subparsers.add_parser(
            'unset',
            help="""Comment out the #define for SYMBOL. Do nothing if none is present.""")
        parser_unset.add_argument('symbol', metavar='SYMBOL')
        parser_unset_all = self.subparsers.add_parser(
            'unset-all',
            help="""Comment out all #define whose name contains a match for REGEX.""")
        parser_unset_all.add_argument('regexs', metavar='REGEX', nargs='*')

    def custom_parser_options(self):
        """Adds custom options for the parser. Designed for overridden by descendant."""
        pass

    def main(self):
        """Common main fuction for config manipulation tool."""

        if self.parser_args.command is None:
            self.parser.print_help()
            return 1
        if self.parser_args.command == 'get':
            if self.parser_args.symbol in self.config:
                value = self.config[self.parser_args.symbol]
                if value:
                    sys.stdout.write(value + '\n')
            return 0 if self.parser_args.symbol in self.config else 1
        elif self.parser_args.command == 'set':
            if not self.parser_args.force and self.parser_args.symbol not in self.config.settings:
                sys.stderr.write(
                    "A #define for the symbol {} was not found in {}\n"
                    .format(self.parser_args.symbol,
                            self.config.filename(self.parser_args.symbol)))
                return 1
            self.config.set(self.parser_args.symbol, value=self.parser_args.value)
        elif self.parser_args.command == 'set-all':
            self.config.change_matching(self.parser_args.regexs, True)
        elif self.parser_args.command == 'unset':
            self.config.unset(self.parser_args.symbol)
        elif self.parser_args.command == 'unset-all':
            self.config.change_matching(self.parser_args.regexs, False)
        else:
            self.config.adapt(self.parser_args.adapter)
        self.config.write(self.parser_args.write)

        return 0
