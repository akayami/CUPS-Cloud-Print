#    CUPS Cloudprint - Print via Google Cloud Print
#    Copyright (C) 2011 Simon Cadman
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation, either version 3 of the License, or
#    (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License
#    along with this program.  If not, see <http://www.gnu.org/licenses/>.
import cups
import json
import urllib
import os
import mimetools
import re
import hashlib
import subprocess
import logging
from auth import Auth
from urlparse import urlparse
from ccputils import Utils
from printer import Printer


class PrinterManager:
    BOUNDARY = mimetools.choose_boundary()
    CRLF = '\r\n'
    PROTOCOL = 'cloudprint://'
    requestors = None
    requestor = None
    cachedPrinterDetails = {}
    reservedCapabilityWords = set((
        'Duplex', 'Resolution', 'Attribute', 'Choice', 'ColorDevice', 'ColorModel', 'ColorProfile',
        'Copyright', 'CustomMedia', 'Cutter', 'Darkness', 'DriverType', 'FileName', 'Filter',
        'Filter', 'Finishing', 'Font', 'Group', 'HWMargins', 'InputSlot', 'Installable',
        'LocAttribute', 'ManualCopies', 'Manufacturer', 'MaxSize', 'MediaSize', 'MediaType',
        'MinSize', 'ModelName', 'ModelNumber', 'Option', 'PCFileName', 'SimpleColorProfile',
        'Throughput', 'UIConstraints', 'VariablePaperSize', 'Version', 'Color', 'Background',
        'Stamp', 'DestinationColorProfile'
    ))
    URIFormatLatest = 1
    URIFormat20140307 = 2
    URIFormat20140210 = 3
    backendDescription =\
        'network %s "%s" "Google Cloud Print" "MFG:Google;MDL:Cloud Print;DES:GoogleCloudPrint;"'

    def __init__(self, requestors):
        """Create an instance of PrinterManager, with authorised requestor

        Args:
          requestors: list or CloudPrintRequestor instance, A list of
          requestors, or a single requestor to use for all Cloud Print
          requests.
        """
        if requestors is not None:
            if isinstance(requestors, list):
                self.requestors = requestors
            else:
                self.requestors = [requestors]

    def getCUPSPrintersForAccount(self, account):
        connection = cups.Connection()
        cupsprinters = connection.getPrinters()
        accountPrinters = []
        for cupsprinter in cupsprinters:
            id, requestor = self.getPrinterIDByURI(cupsprinters[cupsprinter]['device-uri'])
            if id is not None and requestor is not None:
                if requestor.getAccount() == account:
                    accountPrinters.append(cupsprinters[cupsprinter])
        return accountPrinters, connection

    def getPrinters(self, accountName=None):
        """Fetch a list of printers

        Returns:
          list: list of printers for the accounts.
        """
        if not hasattr(self, '_printers'):
            self._printers = []
            for requestor in self.requestors:
                if accountName is not None and accountName != requestor.getAccount():
                    continue

                responseobj = requestor.search()

                if 'printers' in responseobj:
                    for printer_info in responseobj['printers']:
                        self._printers.append(Printer(printer_info, requestor))

        return self._printers

    def sanitizePrinterName(self, name):
        """Sanitizes printer name for CUPS

        Args:
          name: string, name of printer from Google Cloud Print

        Returns:
          string: CUPS-friendly name for the printer
        """
        return re.sub('[^a-zA-Z0-9\-_]', '', name.encode('ascii', 'replace').replace(' ', '_'))

    def addPrinter(self, printername, uri, connection, ppd=None):
        """Adds a printer to CUPS

        Args:
          printername: string, name of the printer to add
          uri: string, uri of the Cloud Print device
          connection: connection, CUPS connection

        Returns:
          None
        """
        # fix printer name
        printername = self.sanitizePrinterName(printername)
        result = None
        try:
            if ppd is None:
                ppdid = 'MFG:GOOGLE;DRV:GCP;CMD:POSTSCRIPT;MDL:' + uri + ';'
                ppds = connection.getPPDs(ppd_device_id=ppdid)
                printerppdname, printerppd = ppds.popitem()
            else:
                printerppdname = ppd
            result = connection.addPrinter(
                name=printername, ppdname=printerppdname, info=printername,
                location='Google Cloud Print', device=uri)
            connection.enablePrinter(printername)
            connection.acceptJobs(printername)
            connection.setPrinterShared(printername, False)
        except Exception as error:
            result = error
        if result is None:
            print "Added " + printername
            return True
        else:
            print "Error adding: " + printername, result
            return False

    @staticmethod
    def _getPrinterIdFromURI(uristring):
        uri = urlparse(uristring)
        return uri.path.split('/')[1]

    def parseLegacyURI(self, uristring, requestors):
        """Parses previous CUPS Cloud Print URIs, only used for upgrades

        Args:
          uristring: string, uri of the Cloud Print device

        Returns:
          string: account name
          string: google cloud print printer name
          string: google cloud print printer id
          int: format id
        """
        printerName = None
        accountName = None
        printerId = None
        uri = urlparse(uristring)
        pathparts = uri.path.strip('/').split('/')
        if len(pathparts) == 2:
            formatId = PrinterManager.URIFormat20140307
            printerId = urllib.unquote(pathparts[1])
            accountName = urllib.unquote(pathparts[0])
            printerName = urllib.unquote(uri.netloc)
        else:
            if urllib.unquote(uri.netloc) not in Auth.GetAccountNames(requestors):
                formatId = PrinterManager.URIFormat20140210
                printerName = urllib.unquote(uri.netloc)
                accountName = urllib.unquote(pathparts[0])
            else:
                formatId = PrinterManager.URIFormatLatest
                printerId = urllib.unquote(pathparts[0])
                printerName = None
                accountName = urllib.unquote(uri.netloc)

        return accountName, printerName, printerId, formatId

    def findRequestorForAccount(self, account):
        """Searches the requestors in the printer object for the requestor for a specific account

        Args:
          account: string, account name
        Return:
          requestor: Single requestor object for the account, or None if no account found
        """
        for requestor in self.requestors:
            if requestor.getAccount() == account:
                return requestor

    def getPrinterByURI(self, uri):
        printerid = self._getPrinterIdFromURI(uri)
        for printer in self.getPrinters():
            if printer['id'] == printerid:
                return printer
        return None

    def getPrinterIDByDetails(self, account, printername, printerid):
        """Gets printer id and requestor by printer

        Args:
          uri: string, printer uri
        Return:
          printer id: Single requestor object for the account, or None if no account found
          requestor: Single requestor object for the account
        """
        # find requestor based on account
        requestor = self.findRequestorForAccount(urllib.unquote(account))
        if requestor is None:
            return None, None

        if printerid is not None:
            return printerid, requestor
        else:
            return None, None

    def getPrinterDetails(self, printerid):
        """Gets details about printer from Google

        Args:
          printerid: string, Google printer id
        Return:
          list: data from Google
        """
        if printerid not in self.cachedPrinterDetails:
            printerdetails = self.requestor.printer(printerid)
            self.cachedPrinterDetails[printerid] = printerdetails
        else:
            printerdetails = self.cachedPrinterDetails[printerid]
        return printerdetails

    def getOverrideCapabilities(self, overrideoptionsstring):
        overrideoptions = overrideoptionsstring.split(' ')
        overridecapabilities = {}

        ignorecapabilities = ['Orientation']
        for optiontext in overrideoptions:
            if '=' in optiontext:
                optionparts = optiontext.split('=')
                option = optionparts[0]
                if option in ignorecapabilities:
                    continue

                value = optionparts[1]
                overridecapabilities[option] = value

            # landscape
            if optiontext == 'landscape' or optiontext == 'nolandscape':
                overridecapabilities['Orientation'] = 'Landscape'

        return overridecapabilities

    def getCapabilitiesDict(
            self, attrs, printercapabilities, overridecapabilities):
        capabilities = {"capabilities": []}
        for attr in attrs:
            if attr['name'].startswith('Default'):
                # gcp setting, reverse back to GCP capability
                gcpname = None
                hashname = attr['name'].replace('Default', '')

                # find item name from hashes
                gcpoption = None
                addedCapabilities = []
                for capability in printercapabilities:
                    if hashname == self.getInternalName(capability, 'capability'):
                        gcpname = capability['name']
                        for option in capability['options']:
                            internalCapability = self.getInternalName(
                                option, 'option', gcpname, addedCapabilities)
                            addedCapabilities.append(internalCapability)
                            if attr['value'] == internalCapability:
                                gcpoption = option['name']
                                break
                        addedOptions = []
                        for overridecapability in overridecapabilities:
                            if 'Default' + overridecapability == attr['name']:
                                selectedoption = overridecapabilities[
                                    overridecapability]
                                for option in capability['options']:
                                    internalOption = self.getInternalName(
                                        option, 'option', gcpname, addedOptions)
                                    addedOptions.append(internalOption)
                                    if selectedoption == internalOption:
                                        gcpoption = option['name']
                                        break
                                break
                        break

                # hardcoded to feature type temporarily
                if gcpname is not None and gcpoption is not None:
                    capabilities['capabilities'].append(
                        {'type': 'Feature', 'name': gcpname, 'options': [{'name': gcpoption}]})
        return capabilities
