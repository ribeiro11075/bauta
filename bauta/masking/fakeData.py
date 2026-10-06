"""The lists the `fake*` masking strategies pick from: names, cities, streets
and companies, by locale -- the original ones, and below them the longer ones
`lists: 2` picks from.

Data only; the fake* strategies in `strategies` use it, and the native masker is
handed the original lists, so this is the one copy of them. Changing an entry
changes every mask already made from its list -- mask-rs/vectors/reference.json
records them all, in every locale, so a change is deliberate.
"""
from __future__ import annotations

from typing import Dict, FrozenSet, NamedTuple, Tuple


FIRST_NAMES = (
    'Ada', 'Alan', 'Alice', 'Amara', 'Andre', 'Anya', 'Arjun', 'Beatriz', 'Bruno', 'Camila', 'Carlos', 'Chen', 'Chloe', 'Dara', 'David',
    'Diego', 'Elena', 'Elif', 'Emeka', 'Emma', 'Ethan', 'Fatima', 'Felix', 'Freya', 'Grace', 'Hana', 'Hugo', 'Ines', 'Isaac', 'Ivan',
    'Jada', 'James', 'Jin', 'Jonas', 'Kai', 'Kofi', 'Lara', 'Leo', 'Lina', 'Lucas', 'Maya', 'Mateo', 'Mei', 'Mila', 'Nadia', 'Noah',
    'Nora', 'Omar', 'Oscar', 'Priya', 'Quinn', 'Rafael', 'Rosa', 'Sami', 'Sara', 'Theo', 'Uma', 'Victor', 'Wren', 'Yara', 'Yusuf', 'Zoe',
    )

LAST_NAMES = (
    'Abara', 'Alvarez', 'Anand', 'Bauer', 'Becker', 'Bianchi', 'Brooks', 'Castillo', 'Chandra', 'Costa', 'Dahl', 'Diallo', 'Dubois',
    'Eriksen', 'Ferreira', 'Fischer', 'Garcia', 'Haddad', 'Hansen', 'Hayes', 'Ibrahim', 'Ito', 'Jensen', 'Kaplan', 'Kim', 'Kowalski',
    'Larsen', 'Lopes', 'Mendes', 'Moreau', 'Murphy', 'Nakamura', 'Novak', 'Okafor', 'Olsen', 'Park', 'Patel', 'Perez', 'Quinlan',
    'Reyes', 'Rossi', 'Santos', 'Schmidt', 'Silva', 'Singh', 'Sousa', 'Tanaka', 'Torres', 'Vargas', 'Varga', 'Wagner', 'Walsh',
    'Weber', 'Wong', 'Yamada', 'Young', 'Zhang', 'Ziegler', 'Adeyemi', 'Bergstrom', 'Carvalho', 'Duarte', 'Falk', 'Lindqvist',
    )

CITIES = (
    'Ashford', 'Bayview', 'Brookfield', 'Cedar Falls', 'Clearwater', 'Crestwood', 'Eastport', 'Elmstead', 'Fairhaven', 'Glenmoor',
    'Greystone', 'Hartwell', 'Highbridge', 'Kingsley', 'Lakemont', 'Maple Grove', 'Marlow', 'Millbrook', 'Northgate', 'Oakridge',
    'Pinecrest', 'Port Albany', 'Ravenswood', 'Redcliff', 'Riverton', 'Rosedale', 'Sandhurst', 'Silverton', 'Southwick', 'Stonebridge',
    'Thornbury', 'Westbrook',
    )

COMPANY_WORDS = (
    'Acorn', 'Apex', 'Beacon', 'Blue Harbor', 'Brightline', 'Cobalt', 'Copperleaf', 'Evergreen', 'Fieldstone', 'Granite', 'Harborview',
    'Ironwood', 'Juniper', 'Keystone', 'Lighthouse', 'Meridian', 'Northwind', 'Oakline', 'Pioneer', 'Quarry', 'Redwood', 'Summit',
    'Tidewater', 'Vantage',
    )

COMPANY_SUFFIXES = ('Analytics', 'Group', 'Holdings', 'Industries', 'Labs', 'Logistics', 'Partners', 'Systems')

STREET_NAMES = (
    'Ash', 'Birch', 'Bridge', 'Canal', 'Cedar', 'Chapel', 'Church', 'Elm', 'Forest', 'Garden', 'Hill', 'Lake', 'Maple', 'Meadow',
    'Mill', 'Oak', 'Orchard', 'Park', 'Pine', 'River', 'School', 'Spring', 'Station', 'Willow',
    )

STREET_SUFFIXES = ('Street', 'Avenue', 'Road', 'Lane', 'Way', 'Drive', 'Court', 'Place')


class Locale(NamedTuple):
    """Names, places and address layout for one country's fake data.

    `address` is a format taking `number`, `street` (from `streets`) and `kind`
    (from `streetKinds`) -- the part that varies most between countries.
    """

    firstNames: Tuple[str, ...]
    lastNames: Tuple[str, ...]
    cities: Tuple[str, ...]
    streets: Tuple[str, ...]
    streetKinds: Tuple[str, ...]
    address: str
    companySuffixes: Tuple[str, ...]


def _words(text: str) -> Tuple[str, ...]:

    return tuple(word.strip() for word in text.split(',') if word.strip())


LOCALES: Dict[str, Locale] = {
    'en_US': Locale(
        _words('James, Mary, Robert, Patricia, John, Jennifer, Michael, Linda, David, Elizabeth, William, Barbara, Richard, Susan, '
               'Joseph, Jessica, Thomas, Sarah, Charles, Karen, Christopher, Lisa, Daniel, Nancy, Matthew, Betty, Anthony, Sandra'),
        _words('Smith, Johnson, Williams, Brown, Jones, Garcia, Miller, Davis, Rodriguez, Martinez, Hernandez, Lopez, Gonzalez, '
               'Wilson, Anderson, Thomas, Taylor, Moore, Jackson, Martin, Lee, Perez, Thompson, White, Harris, Sanchez, Clark, Lewis'),
        _words('Springfield, Riverside, Franklin, Greenville, Clinton, Fairview, Salem, Madison, Georgetown, Arlington, Ashland, '
               'Burlington, Manchester, Oxford, Milton, Clayton, Dayton, Lexington, Milford, Bristol'),
        _words('Main, Oak, Pine, Maple, Cedar, Elm, Washington, Lake, Hill, Park, Walnut, Spring'),
        _words('Street, Avenue, Road, Drive, Lane, Court, Boulevard, Way'),
        '{number} {street} {kind}', _words('Inc., LLC, Corp., Co.')),
    'en_GB': Locale(
        _words('Oliver, Amelia, George, Isla, Harry, Ava, Jack, Mia, Jacob, Emily, Charlie, Sophie, Thomas, Grace, Oscar, Lily, '
               'William, Freya, James, Evie, Alfie, Ella, Henry, Poppy'),
        _words('Smith, Jones, Taylor, Brown, Williams, Wilson, Johnson, Davies, Robinson, Wright, Thompson, Evans, Walker, White, '
               'Roberts, Green, Hall, Wood, Jackson, Clarke, Hughes, Edwards, Turner, Hill'),
        _words('Bradford, Chester, Durham, Exeter, Harrogate, Kendal, Lincoln, Ludlow, Norwich, Reading, Salisbury, Stafford, Truro, '
               'Wells, Whitby, Winchester, Worcester, York, Bath, Carlisle'),
        _words('High, Church, Station, Victoria, Park, Mill, Queen, King, School, London, Manor, Chapel'),
        _words('Street, Road, Lane, Close, Avenue, Way, Gardens, Crescent'),
        '{number} {street} {kind}', _words('Ltd, PLC, LLP')),
    'de_DE': Locale(
        _words('Lukas, Anna, Leon, Mia, Finn, Emma, Jonas, Hannah, Paul, Lea, Felix, Lena, Maximilian, Marie, Elias, Sophie, Noah, '
               'Laura, Ben, Julia, Tim, Lisa, Jan, Katharina'),
        _words('Müller, Schmidt, Schneider, Fischer, Weber, Meyer, Wagner, Becker, Schulz, Hoffmann, Schäfer, Koch, Bauer, Richter, '
               'Klein, Wolf, Schröder, Neumann, Schwarz, Zimmermann, Braun, Krüger, Hofmann, Hartmann'),
        _words('Aachen, Bamberg, Bielefeld, Bonn, Celle, Darmstadt, Erfurt, Freiburg, Göttingen, Heidelberg, Kassel, Kiel, Konstanz, '
               'Lübeck, Mainz, Münster, Passau, Regensburg, Trier, Ulm'),
        _words('Haupt, Bahnhof, Garten, Schul, Kirch, Linden, Berg, Wald, Mühlen, Dorf, Birken, Rosen'),
        _words('straße, weg, gasse, allee, ring, platz'),
        '{street}{kind} {number}', _words('GmbH, AG, KG, GmbH & Co. KG')),
    'fr_FR': Locale(
        _words('Gabriel, Emma, Léo, Jade, Raphaël, Louise, Arthur, Alice, Louis, Chloé, Lucas, Lina, Adam, Rose, Jules, Léa, Hugo, '
               'Anna, Maël, Mila, Nathan, Julia, Paul, Inès'),
        _words('Martin, Bernard, Dubois, Thomas, Robert, Richard, Petit, Durand, Leroy, Moreau, Simon, Laurent, Lefebvre, Michel, '
               'Garcia, David, Bertrand, Roux, Vincent, Fournier, Morel, Girard, André, Mercier'),
        _words('Amiens, Angers, Annecy, Avignon, Besançon, Brest, Caen, Colmar, Dijon, Grenoble, Limoges, Metz, Nancy, Nîmes, '
               'Orléans, Pau, Poitiers, Reims, Rouen, Tours'),
        _words("de la Paix, des Lilas, Victor Hugo, de la Gare, du Moulin, des Écoles, de l'Église, Pasteur, Jean Jaurès, "
               'du Château, des Tilleuls, de la République'),
        _words('rue, avenue, boulevard, place, allée, chemin'),
        '{number} {kind} {street}', _words('SARL, SAS, SA, EURL')),
    'es_ES': Locale(
        _words('Hugo, Lucía, Martín, Sofía, Daniel, Martina, Pablo, María, Alejandro, Julia, Lucas, Paula, Álvaro, Valeria, Adrián, '
               'Emma, Mateo, Daniela, David, Carla, Diego, Alba, Javier, Noa'),
        _words('García, Rodríguez, González, Fernández, López, Martínez, Sánchez, Pérez, Gómez, Martín, Jiménez, Ruiz, Hernández, '
               'Díaz, Moreno, Muñoz, Álvarez, Romero, Alonso, Gutiérrez, Navarro, Torres, Domínguez, Vázquez'),
        _words('Albacete, Alicante, Badajoz, Burgos, Cáceres, Cádiz, Córdoba, Gijón, Girona, Granada, Huelva, León, Logroño, Lugo, '
               'Oviedo, Salamanca, Santander, Segovia, Toledo, Zamora'),
        _words('Mayor, Real, del Sol, de la Paz, Nueva, del Carmen, San Juan, de la Iglesia, del Mar, de Cervantes, Colón, de Goya'),
        _words('Calle, Avenida, Plaza, Paseo, Camino, Ronda'),
        '{kind} {street}, {number}', _words('S.L., S.A., S.L.U.')),
    'pt_BR': Locale(
        _words('Miguel, Helena, Arthur, Alice, Gael, Laura, Heitor, Maria, Theo, Valentina, Davi, Heloísa, Gabriel, Sophia, Bernardo, '
               'Manuela, Samuel, Júlia, João, Isabela, Pedro, Lívia, Lucas, Beatriz'),
        _words('Silva, Santos, Oliveira, Souza, Rodrigues, Ferreira, Alves, Pereira, Lima, Gomes, Costa, Ribeiro, Martins, Carvalho, '
               'Almeida, Lopes, Soares, Fernandes, Vieira, Barbosa, Rocha, Dias, Nascimento, Andrade'),
        _words('Aracaju, Belém, Blumenau, Campinas, Cuiabá, Curitiba, Florianópolis, Goiânia, Joinville, Londrina, Maceió, Manaus, '
               'Natal, Niterói, Olinda, Petrópolis, Santos, Sorocaba, Uberlândia, Vitória'),
        _words('das Flores, São João, Sete de Setembro, XV de Novembro, das Palmeiras, Santa Luzia, do Comércio, Brasil, da Paz, '
               'Dom Pedro II, das Acácias, Tiradentes'),
        _words('Rua, Avenida, Travessa, Praça, Alameda, Estrada'),
        '{kind} {street}, {number}', _words('Ltda., S.A., ME')),
    'it_IT': Locale(
        _words('Leonardo, Sofia, Francesco, Aurora, Tommaso, Giulia, Edoardo, Ginevra, Alessandro, Beatrice, Lorenzo, Alice, Mattia, '
               'Vittoria, Gabriele, Emma, Riccardo, Ludovica, Andrea, Matilde, Diego, Chiara, Nicolò, Anna'),
        _words('Rossi, Russo, Ferrari, Esposito, Bianchi, Romano, Colombo, Ricci, Marino, Greco, Bruno, Gallo, Conti, De Luca, '
               'Mancini, Costa, Giordano, Rizzo, Lombardi, Moretti, Barbieri, Fontana, Santoro, Mariani'),
        _words('Ancona, Arezzo, Bergamo, Bologna, Brescia, Cagliari, Como, Cremona, Ferrara, Lecce, Lucca, Mantova, Modena, Padova, '
               'Parma, Perugia, Pisa, Ravenna, Siena, Trento'),
        _words('Roma, Garibaldi, Mazzini, Dante, Verdi, Cavour, Marconi, dei Mille, della Libertà, Vittorio Emanuele, San Francesco, '
               'del Popolo'),
        _words('Via, Viale, Piazza, Corso, Vicolo, Largo'),
        '{kind} {street} {number}', _words('S.r.l., S.p.A., S.a.s., S.n.c.')),
    'nl_NL': Locale(
        _words('Noah, Emma, Luca, Julia, Sem, Mila, Lucas, Tess, Levi, Sophie, Finn, Zoë, Daan, Sara, Milan, Nora, Bram, Yara, Mees, '
               'Eva, Jesse, Liv, Thijs, Anna'),
        _words('de Jong, Jansen, de Vries, van den Berg, van Dijk, Bakker, Janssen, Visser, Smit, Meijer, de Boer, Mulder, de Groot, '
               'Bos, Vos, Peters, Hendriks, van Leeuwen, Dekker, Brouwer, de Wit, Dijkstra, Smits, de Graaf'),
        _words('Alkmaar, Amersfoort, Apeldoorn, Arnhem, Breda, Delft, Deventer, Dordrecht, Enschede, Gouda, Groningen, Haarlem, '
               'Leeuwarden, Leiden, Maastricht, Nijmegen, Tilburg, Utrecht, Zwolle, Zaandam'),
        _words('Kerk, Molen, School, Dorps, Linden, Beuken, Stations, Wilhelmina, Juliana, Nieuwe, Oranje, Eiken'),
        _words('straat, weg, laan, plein, singel, gracht'),
        '{street}{kind} {number}', _words('B.V., N.V., V.O.F.')),
    }

# The lists used without a `locale` option: a deliberately international mix.
# Kept exactly as they were, since changing them would change every mask
# already written with them.
DEFAULT_LOCALE = Locale(FIRST_NAMES, LAST_NAMES, CITIES, STREET_NAMES, STREET_SUFFIXES, '{number} {street} {kind}', COMPANY_SUFFIXES)


# --- lists: 2 ---------------------------------------------------------------
#
# Longer lists, first names by gender. Only Python reads them: the native
# masker covers `lists: 1`.

class LargeLocale(NamedTuple):
    """One locale's lists for `lists: 2`. A name in both first-name lists
    is left in one, and tells neither gender.
    """

    femaleNames: Tuple[str, ...]
    maleNames: Tuple[str, ...]
    lastNames: Tuple[str, ...]
    cities: Tuple[str, ...]


def _uniqueWords(text: str) -> Tuple[str, ...]:
    """Comma-separated entries, each once, in the order first written."""

    return tuple(dict.fromkeys(word.strip() for word in text.split(',') if word.strip()))


LARGE_LOCALES: Dict[str, LargeLocale] = {
    'en_US': LargeLocale(
        _uniqueWords('Mary, Patricia, Jennifer, Linda, Elizabeth, Barbara, Susan, Jessica, Sarah, Karen, Lisa, Nancy, Betty, Sandra, '
               'Margaret, Ashley, Kimberly, Emily, Donna, Michelle, Carol, Amanda, Melissa, Deborah, Stephanie, Dorothy, Rebecca, '
               'Sharon, Laura, Cynthia, Amy, Kathleen, Angela, Shirley, Brenda, Emma, Anna, Pamela, Nicole, Samantha, Katherine, '
               'Christine, Helen, Debra, Rachel, Carolyn, Janet, Maria, Catherine, Heather, Diane, Olivia, Julie, Joyce, Victoria, '
               'Ruth, Virginia, Lauren, Kelly, Christina, Joan, Evelyn, Judith, Andrea, Hannah, Megan, Cheryl, Jacqueline, Martha, '
               'Madison, Teresa, Gloria, Sara, Janice, Ann, Kathryn, Abigail, Sophia, Frances, Jean, Alice, Judy, Isabella, Julia, '
               'Grace, Amber, Denise, Danielle, Marilyn, Beverly, Charlotte, Natalie, Theresa, Diana, Brittany, Doris, Kayla, Alexis, '
               'Lori, Marie, Tiffany, Kristen, Rose, Lillian, Ava, Mia, Harper, Ella, Scarlett, Chloe, Aria, Layla, Zoey, Nora, Lily, '
               'Hazel, Violet, Aurora, Savannah, Audrey, Brooklyn, Bella, Claire, Lucy, Paisley, Everly, Caroline, Nova, Emilia, '
               'Maya, Willow, Naomi, Aaliyah, Elena, Ariana, Allison, Gabriella, Alexa, Madelyn, Cora, Ruby, Eva, Autumn, Adeline, '
               'Hailey, Gianna, Valentina, Isla, Eliana, Stella, Leah, Addison, Penelope, Ellie, Josephine, Delilah, Ivy'),
        _uniqueWords('James, Robert, John, Michael, David, William, Richard, Joseph, Thomas, Christopher, Charles, Daniel, Matthew, '
               'Anthony, Mark, Donald, Steven, Andrew, Paul, Joshua, Kenneth, Kevin, Brian, George, Timothy, Ronald, Jason, Edward, '
               'Jeffrey, Ryan, Jacob, Gary, Nicholas, Eric, Jonathan, Stephen, Larry, Justin, Scott, Brandon, Benjamin, Samuel, '
               'Gregory, Alexander, Patrick, Frank, Raymond, Jack, Dennis, Jerry, Tyler, Aaron, Jose, Adam, Nathan, Henry, Zachary, '
               'Douglas, Peter, Kyle, Noah, Ethan, Jeremy, Walter, Christian, Keith, Roger, Terry, Austin, Sean, Gerald, Carl, Harold, '
               'Dylan, Arthur, Lawrence, Jordan, Jesse, Bryan, Billy, Bruce, Gabriel, Joe, Logan, Alan, Juan, Albert, Willie, Elijah, '
               'Wayne, Randy, Vincent, Mason, Roy, Ralph, Bobby, Russell, Bradley, Philip, Eugene, Liam, Oliver, Lucas, Levi, '
               'Sebastian, Mateo, Jackson, Owen, Theodore, Aiden, Luke, Grayson, Leo, Isaac, Lincoln, Hudson, Ezra, Asher, Caleb, '
               'Josiah, Wyatt, Carter, Julian, Hunter, Connor, Landon, Colton, Cameron, Evan, Adrian, Miles, Nolan, Easton, Dominic, '
               'Cooper, Roman, Xavier, Ian, Axel, Silas, Brooks, Declan, Weston, Micah, Ryder, Everett, Jameson, Wesley, Harrison'),
        _uniqueWords('Smith, Johnson, Williams, Brown, Jones, Garcia, Miller, Davis, Rodriguez, Martinez, Hernandez, Lopez, Gonzalez, '
               'Wilson, Anderson, Thomas, Taylor, Moore, Jackson, Martin, Lee, Perez, Thompson, White, Harris, Sanchez, Clark, Ramirez, '
               'Lewis, Robinson, Walker, Young, Allen, King, Wright, Scott, Torres, Nguyen, Hill, Flores, Green, Adams, Nelson, Baker, '
               'Hall, Rivera, Campbell, Mitchell, Carter, Roberts, Gomez, Phillips, Evans, Turner, Diaz, Parker, Cruz, Edwards, '
               'Collins, Reyes, Stewart, Morris, Morales, Murphy, Cook, Rogers, Gutierrez, Ortiz, Morgan, Cooper, Peterson, Bailey, '
               'Reed, Kelly, Howard, Ramos, Kim, Cox, Ward, Richardson, Watson, Brooks, Chavez, Wood, James, Bennett, Gray, Mendoza, '
               'Ruiz, Hughes, Price, Alvarez, Castillo, Sanders, Patel, Myers, Long, Ross, Foster, Jimenez, Powell, Jenkins, Perry, '
               'Russell, Sullivan, Bell, Coleman, Butler, Henderson, Barnes, Gonzales, Fisher, Vasquez, Simmons, Romero, Jordan, '
               'Patterson, Alexander, Hamilton, Graham, Reynolds, Griffin, Wallace, Moreno, West, Cole, Hayes, Bryant, Herrera, '
               'Gibson, Ellis, Tran, Medina, Aguilar, Stevens, Murray, Ford, Castro, Marshall, Owens, Harrison, Fernandez, McDonald, '
               'Woods, Washington, Kennedy, Wells, Vargas, Henry, Chen, Freeman, Webb, Tucker, Guzman, Burns, Crawford, Olson, '
               'Simpson, Porter, Hunter, Gordon, Mendez, Silva, Shaw, Snyder, Mason, Dixon, Munoz, Hunt, Hicks, Holmes, Palmer, '
               'Wagner, Black, Robertson, Boyd, Rose, Stone, Salazar, Fox, Warren, Mills, Meyer, Rice, Schmidt, Garza, Daniels, '
               'Ferguson, Nichols, Stephens, Soto, Weaver, Ryan, Gardner, Payne, Grant, Dunn, Kelley, Spencer, Hawkins, Arnold, '
               'Pierce, Vazquez, Hansen, Peters, Santos, Hart, Bradley, Knight, Elliott, Cunningham, Duncan, Armstrong, Hudson, '
               'Carroll, Lane, Riley, Andrews, Alvarado, Ray, Delgado, Berry, Perkins, Hoffman, Johnston, Matthews, Pena, Richards, '
               'Contreras, Willis, Carpenter, Lawrence, Sandoval, Guerrero, George, Chapman, Rios, Estrada, Ortega, Watkins, Greene, '
               'Nunez, Wheeler, Valdez, Harper, Burke, Larson, Santiago, Maldonado, Morrison, Franklin, Carlson, Austin, Dominguez, '
               'Carr, Lawson, Jacobs, OBrien, Lynch, Singh, Vega, Bishop, Montgomery, Oliver, Jensen, Harvey, Williamson, Gilbert, '
               'Dean, Sims, Espinoza, Howell, Li, Wong, Reid, Hanson, Le, McCoy, Garrett, Burton, Fuller, Wang, Weber, Welch, Rojas, '
               'Lucas, Marquez, Fields, Park, Yang, Little, Banks, Padilla, Day, Walsh, Bowman, Schultz, Luna, Fowler, Mejia, Davidson'),
        _uniqueWords('Springfield, Riverside, Franklin, Greenville, Clinton, Fairview, Salem, Madison, Georgetown, Arlington, Ashland, '
               'Burlington, Manchester, Oxford, Milton, Clayton, Dayton, Lexington, Milford, Bristol, Auburn, Bloomington, Bozeman, '
               'Boulder, Cambridge, Canton, Carson City, Chapel Hill, Charleston, Columbia, Concord, Danville, Dover, Durham, Eugene, '
               'Fayetteville, Flagstaff, Fort Collins, Frederick, Gainesville, Hamilton, Harrisburg, Helena, Huntsville, Jackson, '
               'Jamestown, Kingston, Knoxville, Lafayette, Lancaster, Lansing, Lawrence, Lincoln, Macon, Marion, Medford, Middletown, '
               'Missoula, Monroe, Montgomery, Newport, Norman, Ogden, Olympia, Peoria, Plymouth, Portland, Princeton, Provo, Quincy, '
               'Raleigh, Reno, Richmond, Rochester, Santa Fe, Savannah, Spokane, Tacoma, Tallahassee, Troy, Tulsa, Union City, '
               'Waco, Warren, Wilmington, Winchester, Worcester, Yorktown')),

    'en_GB': LargeLocale(
        _uniqueWords('Olivia, Amelia, Isla, Ava, Mia, Ivy, Lily, Isabella, Rosie, Sophia, Grace, Willow, Freya, Florence, Emily, Ella, '
               'Poppy, Evie, Elsie, Charlotte, Evelyn, Sienna, Sofia, Daisy, Phoebe, Sophie, Alice, Harper, Matilda, Ruby, Emilia, '
               'Maya, Millie, Isabelle, Ada, Arabella, Eva, Imogen, Esme, Jessica, Lucy, Thea, Scarlett, Erin, Ellie, Hallie, Holly, '
               'Bonnie, Molly, Eliza, Robyn, Penelope, Hannah, Zara, Aria, Maisie, Beatrice, Clara, Darcie, Georgia, Lottie, Martha, '
               'Edith, Mabel, Nancy, Orla, Rose, Iris, Violet, Elizabeth, Amber, Megan, Chloe, Lauren, Rebecca, Bethany, Gemma, '
               'Hayley, Kirsty, Leanne, Natalie, Rachel, Samantha, Sarah, Victoria, Zoe, Abigail, Amy, Anna, Caitlin, '
               'Eleanor, Faye, Fiona, Gillian, Heather, Helen, Jane, Joanne, Julie, Karen, Katie, Kerry, Laura, Lisa, Louise, Lynne, '
               'Margaret, Mary, Michelle, Nicola, Paula, Rachael, Sally, Sharon, Stephanie, Susan, Tracey, Wendy, Yvonne, Alison, '
               'Angela, Carol, Claire, Debbie, Diane, Donna, Elaine, Emma, Hazel, Jennifer, Joan, Judith, Kathryn, Linda, Pauline'),
        _uniqueWords('Muhammad, Noah, Oliver, Leo, George, Arthur, Oscar, Theodore, Freddie, Archie, Henry, Theo, Alfie, Charlie, Jack, '
               'Thomas, Finley, Lucas, Isaac, Tommy, Teddy, Albie, Edward, Jaxon, Joshua, Harry, Arlo, Reggie, Rory, Ezra, William, '
               'Hudson, Sonny, Elijah, Louie, Jude, Ronnie, Harrison, Max, Mason, Hugo, Albert, Ethan, Toby, Jacob, Jesse, Frankie, '
               'Ellis, Harvey, Louis, Alexander, Joseph, Samuel, Daniel, Benjamin, James, Callum, Connor, Liam, Kieran, Jamie, Ryan, '
               'Lewis, Adam, Matthew, Luke, Ben, Sam, Nathan, Dylan, Kyle, Jordan, Aaron, Owen, Rhys, Gareth, Dean, Wayne, Lee, Craig, '
               'Darren, Gary, Neil, Paul, Mark, Stephen, Andrew, Ian, Simon, David, Christopher, Richard, Robert, Michael, Martin, '
               'Philip, Peter, John, Graham, Keith, Kevin, Colin, Barry, Trevor, Nigel, Malcolm, Gordon, Derek, Alan, Kenneth, '
               'Raymond, Geoffrey, Roger, Clive, Brian, Ralph, Stuart, Duncan, Angus, Fraser, Euan, Hamish, Iain, Alistair, Ewan, '
               'Dafydd, Gethin, Huw, Iwan, Llewellyn, Rhodri, Cian, Declan, Niall, Ronan, Seamus, Brendan, Cormac, Fergus'),
        _uniqueWords('Smith, Jones, Taylor, Brown, Williams, Wilson, Johnson, Davies, Robinson, Wright, Thompson, Evans, Walker, White, '
               'Roberts, Green, Hall, Wood, Jackson, Clarke, Hughes, Edwards, Turner, Hill, Moore, Clark, Harrison, Scott, Young, '
               'Morris, Lee, Watson, Harris, Martin, Cooper, King, Ward, Baker, Phillips, Allen, Morgan, Lewis, James, Bell, Parker, '
               'Bennett, Griffiths, Cook, Kelly, Price, Carter, Mitchell, Shaw, Richardson, Cox, Bailey, Chapman, Webb, Rogers, '
               'Howard, Marshall, Lloyd, Thomas, Palmer, Hunt, Holmes, Mills, Dixon, Murphy, Mason, Grant, Ellis, Richards, Russell, '
               'Graham, Pearson, Knight, Powell, Butler, Barnes, Fisher, Simpson, Stevens, Reynolds, Matthews, Foster, Jenkins, '
               'Gibson, Spencer, Saunders, Owen, Payne, Gray, Ross, Fox, Moss, Lawrence, Holland, Wells, Chambers, Dawson, Barker, '
               'Burton, Pritchard, Atkinson, Hart, Hamilton, Ford, Lane, Kennedy, Walsh, Fraser, Davidson, Stewart, Campbell, '
               'Anderson, Macdonald, Reid, Murray, Ferguson, Henderson, Duncan, Paterson, Robertson, Johnston, Mackenzie, Hunter, '
               'Watt, Wallace, Hay, Ritchie, Sutherland, Morrison, Burns, Grieve, Forbes, Docherty, Maclean, Kerr, Gordon, Rees, '
               'Howells, Vaughan, Pugh, Probert, Prosser, Llewellyn, Bevan, Thorne, Hodgson, Gill, Sharp, Hudson, '
               'Booth, Kaur, Singh, Patel, Khan, Ali, Hussain, Begum, Ahmed, Shah, Rahman, Mahmood, Akhtar, Iqbal, Sharma, Gupta, '
               'Doyle, Byrne, Ryan, OConnor, OBrien, Quinn, McCarthy, Brennan, Gallagher, Lynch, Sullivan, Kavanagh, Nolan, Farrell, '
               'Whitehead, Lowe, Warren, Fletcher, Armstrong, Hayes, Bates, Cartwright, Banks, Cross, Black, Pope, Jordan, Field, '
               'Stone, Day, Riley, Sanders, Cole, Holt, Austin, Archer, Bird, Bishop, Bolton, Bond, Bradley, Brooks, Bryant, Burgess, '
               'Coleman, Collins, Conway, Cunningham, Curtis, Dean, Dunn, Elliott, Fowler, Francis, Fuller, Gardner, Gilbert, '
               'Goodwin, Gregory, Hale, Harding, Hardy, Harper, Hawkins, Heath, Henry, Hicks, Higgins, Hobbs, Hopkins, Horton, '
               'Hutchinson, Jarvis, Kemp, Lamb, Little, Lucas, Mann, Marsh, Miles, Newman, Nicholson, Norris, Osborne, Page, Parry, '
               'Perkins, Poole, Potter, Read, Rowe, Rose, Sheppard, Slater, Steele, Stephenson, Sutton, Tucker, Wade, Walton, Warner'),
        _uniqueWords('Bradford, Chester, Durham, Exeter, Harrogate, Kendal, Lincoln, Ludlow, Norwich, Reading, Salisbury, Stafford, '
               'Truro, Wells, Whitby, Winchester, Worcester, York, Bath, Carlisle, Aberdeen, Ayr, Bangor, Barnsley, Basingstoke, '
               'Bedford, Blackburn, Bolton, Bournemouth, Brighton, Cambridge, Canterbury, Cardiff, Chelmsford, Cheltenham, '
               'Chichester, Colchester, Coventry, Darlington, Derby, Dorchester, Dundee, Eastbourne, Edinburgh, Falkirk, Glasgow, '
               'Gloucester, Guildford, Halifax, Hereford, Huddersfield, Inverness, Ipswich, Kilmarnock, Lancaster, Leeds, Leicester, '
               'Llandudno, Luton, Maidstone, Margate, Newcastle, Northampton, Nottingham, Oxford, Penzance, Perth, Peterborough, '
               'Plymouth, Portsmouth, Preston, Richmond, Ripon, Rochdale, Scarborough, Sheffield, Shrewsbury, Southampton, Stirling, '
               'Sunderland, Swansea, Swindon, Taunton, Telford, Wakefield, Warrington, Watford, Weymouth, Wigan, Windsor, Wrexham')),

    'de_DE': LargeLocale(
        _uniqueWords('Anna, Mia, Emma, Hannah, Lea, Lena, Marie, Sophie, Laura, Julia, Lisa, Katharina, Emilia, Lina, Ella, Clara, Mila, '
               'Leni, Ida, Lia, Frieda, Greta, Mathilda, Charlotte, Maja, Lotta, Amelie, Johanna, Paula, Luisa, Nele, Alina, Leonie, '
               'Lara, Sarah, Vanessa, Jana, Jessica, Jennifer, Nina, Melanie, Sandra, Stefanie, Nicole, Christina, Sabine, Petra, '
               'Andrea, Claudia, Susanne, Birgit, Monika, Ursula, Renate, Brigitte, Ingrid, Gisela, Helga, Elke, Karin, Heike, Silke, '
               'Anja, Tanja, Katrin, Kerstin, Martina, Gabriele, Barbara, Angelika, Christiane, Doris, Elisabeth, Erika, Gertrud, '
               'Hildegard, Irmgard, Jutta, Margarete, Marianne, Rosemarie, Sieglinde, Ulrike, Waltraud, Annika, Franziska, Friederike, '
               'Hanna, Helene, Isabell, Jasmin, Josephine, Kim, Lilly, Magdalena, Merle, Miriam, Nadine, Pia, Rebecca, Ronja, Sina, '
               'Svenja, Theresa, Valentina, Verena, Viktoria, Yvonne, Zoe, Antonia, Carla, Carolin, Daniela, Diana, Eva, Hedwig, '
               'Ilse, Judith, Kathrin, Lucia, Marlene, Michaela, Natalie, Ramona, Regina, Simone, Sonja, Tamara, Ute, Wiebke'),
        _uniqueWords('Lukas, Leon, Finn, Jonas, Paul, Felix, Maximilian, Elias, Noah, Ben, Tim, Jan, Luca, Henry, Emil, Theo, Matteo, '
               'Anton, Karl, Oskar, Jakob, Moritz, Niklas, Julian, Philipp, Fabian, David, Alexander, Sebastian, Tobias, Florian, '
               'Daniel, Michael, Thomas, Andreas, Stefan, Christian, Markus, Martin, Frank, Jürgen, Klaus, Wolfgang, Peter, Uwe, '
               'Dieter, Manfred, Helmut, Werner, Gerhard, Günter, Horst, Heinz, Hans, Karl-Heinz, Rolf, Bernd, Ralf, Jörg, Dirk, '
               'Holger, Torsten, Sven, Matthias, Oliver, Patrick, Dennis, Marcel, Kevin, Pascal, Marco, Dominik, Benjamin, Johannes, '
               'Simon, Lennard, Mats, Ole, Hannes, Malte, Till, Linus, Vincent, Valentin, Konstantin, Leopold, Ludwig, Friedrich, '
               'Wilhelm, Heinrich, Otto, Fritz, Ernst, Hermann, Walter, Kurt, Erich, Gustav, Rudolf, Reinhard, Volker, Joachim, '
               'Rainer, Norbert, Lothar, Siegfried, Detlef, Hartmut, Ulrich, Axel, Carsten, Kai, Lars, Nils, Jens, Timo, Robin, '
               'Marvin, Jannik, Lasse, Henrik, Erik, Arne, Bastian, Benedikt, Clemens, Gregor, Kilian, Leonhard, Quirin, Severin'),
        _uniqueWords('Müller, Schmidt, Schneider, Fischer, Weber, Meyer, Wagner, Becker, Schulz, Hoffmann, Schäfer, Koch, Bauer, '
               'Richter, Klein, Wolf, Schröder, Neumann, Schwarz, Zimmermann, Braun, Krüger, Hofmann, Hartmann, Lange, Schmitt, '
               'Werner, Schmitz, Krause, Meier, Lehmann, Schmid, Schulze, Maier, Köhler, Herrmann, König, Walter, Mayer, Huber, '
               'Kaiser, Fuchs, Peters, Lang, Scholz, Möller, Weiß, Jung, Hahn, Schubert, Vogel, Friedrich, Keller, Günther, Frank, '
               'Berger, Winkler, Roth, Beck, Lorenz, Baumann, Franke, Albrecht, Schuster, Simon, Ludwig, Böhm, Winter, Kraus, Martin, '
               'Schumacher, Krämer, Vogt, Stein, Jäger, Otto, Sommer, Groß, Seidel, Heinrich, Brandt, Haas, Schreiber, Graf, '
               'Schulte, Dietrich, Ziegler, Kuhn, Kühn, Pohl, Engel, Horn, Busch, Bergmann, Thomas, Voigt, Sauer, Arnold, Wolff, '
               'Pfeiffer, Ernst, Lindner, Hübner, Kramer, Franz, Jansen, Peter, Hammer, Götz, Fiedler, Kaufmann, Böttcher, Hesse, '
               'Thiel, Kessler, Seifert, Nagel, Schreiner, Gerlach, Brinkmann, Sander, Wenzel, Schuhmacher, Mertens, '
               'Ritter, Ebert, Marx, Wendt, Paul, Langer, Kurz, Zimmer, Hauser, Kirchner, Witt, Rieger, Bock, Hartwig, Reuter, '
               'Kolb, Ott, Siebert, Behrens, Stahl, Dittrich, Frey, Schilling, Brune, Pietsch, Schütz, Heinz, Reinhardt, Wolter, '
               'Rau, Schlegel, Wilhelm, Ulrich, Michel, Kraft, Busse, Wegner, Schade, Barth, Thiele, Dörr, Fröhlich, Schramm, Heck, '
               'Specht, Bader, Hennig, Kühne, Moser, Riedel, Ackermann, Adam, Bach, Blum, Brückner, Dahl, Eckert, Eberhardt, Fink, '
               'Gärtner, Geiger, Hagen, Hecht, Hirsch, Hofer, Jakob, Kern, Lenz, Lindemann, Lutz, Maurer, Mohr, Nowak, '
               'Ostermann, Pape, Popp, Ruf, Sattler, Scherer, Seitz, Stark, Steiner, Strauß, Theis, Ullrich, Unger, Vetter, Wagener, '
               'Walther, Weise, Wiese, Wirth, Wörner, Zander'),
        _uniqueWords('Aachen, Bamberg, Bielefeld, Bonn, Celle, Darmstadt, Erfurt, Freiburg, Göttingen, Heidelberg, Kassel, Kiel, '
               'Konstanz, Lübeck, Mainz, Münster, Passau, Regensburg, Trier, Ulm, Augsburg, Bayreuth, Bochum, Brandenburg, '
               'Braunschweig, Bremen, Chemnitz, Coburg, Cottbus, Dessau, Detmold, Dortmund, Dresden, Duisburg, Eisenach, Erlangen, '
               'Essen, Flensburg, Frankfurt, Fulda, Gera, Gießen, Görlitz, Gotha, Greifswald, Hagen, Halle, Hamburg, Hameln, Hanau, '
               'Hannover, Heilbronn, Hildesheim, Ingolstadt, Jena, Karlsruhe, Koblenz, Köln, Landshut, Leipzig, Lüneburg, '
               'Magdeburg, Mannheim, Marburg, Meißen, Memmingen, Minden, Nürnberg, Oldenburg, Osnabrück, Paderborn, Potsdam, '
               'Quedlinburg, Rosenheim, Rostock, Saarbrücken, Schwerin, Siegen, Speyer, Stralsund, Stuttgart, Tübingen, Weimar, '
               'Wiesbaden, Wismar, Wolfsburg, Worms, Wuppertal, Würzburg, Zwickau')),

    'fr_FR': LargeLocale(
        _uniqueWords('Emma, Jade, Louise, Alice, Chloé, Lina, Rose, Léa, Anna, Mila, Julia, Inès, Ambre, Mia, Léna, Agathe, Juliette, '
               'Iris, Lou, Zoé, Camille, Sarah, Eva, Romane, Manon, Nina, Charlotte, Margaux, Clémence, Adèle, Victoire, Lucie, '
               'Mathilde, Pauline, Marie, Julie, Laura, Céline, Aurélie, Émilie, Élodie, Sophie, Nathalie, Isabelle, Sandrine, '
               'Valérie, Stéphanie, Christine, Catherine, Sylvie, Martine, Françoise, Monique, Nicole, Brigitte, Chantal, Dominique, '
               'Véronique, Patricia, Corinne, Florence, Karine, Virginie, Delphine, Caroline, Hélène, Anne, Claire, Audrey, Mélanie, '
               'Laetitia, Marion, Justine, Océane, Morgane, Maëlle, Anaïs, Clara, Léonie, Capucine, Apolline, Héloïse, Margot, '
               'Constance, Joséphine, Éléonore, Gabrielle, Garance, Lison, Maëlys, Noémie, Ophélie, Salomé, Solène, Elsa, Elise, '
               'Gaëlle, Jeanne, Madeleine, Marguerite, Odette, Paulette, Simone, Suzanne, Yvette, Germaine, Colette, Danielle, '
               'Denise, Geneviève, Ginette, Huguette, Jacqueline, Josiane, Michèle, Mireille, Odile, Annie, Béatrice, Evelyne, Laure'),
        _uniqueWords('Gabriel, Léo, Raphaël, Arthur, Louis, Lucas, Adam, Jules, Hugo, Maël, Nathan, Paul, Gabin, Sacha, Noah, Tom, '
               'Aaron, Mohamed, Liam, Timéo, Théo, Ethan, Noé, Victor, Martin, Axel, Mathis, Baptiste, Clément, Maxime, Antoine, '
               'Alexandre, Thomas, Nicolas, Julien, Romain, Guillaume, Florian, Kevin, Mickaël, Sébastien, Jérôme, Christophe, '
               'Stéphane, Frédéric, David, Laurent, Olivier, Philippe, Pascal, Thierry, Éric, Patrick, Bruno, Didier, Gilles, Alain, '
               'Bernard, Jacques, Michel, Jean, Pierre, André, René, Daniel, Claude, Gérard, Serge, Yves, Marcel, Roger, Henri, '
               'Lucien, Raymond, Robert, Georges, Fernand, Maurice, Émile, Gaston, Léon, Albert, Joseph, Marius, Eugène, Augustin, '
               'Achille, Anatole, Basile, Côme, Édouard, Félix, Gaspard, Isaac, Marceau, Nolan, Oscar, Simon, '
               'Valentin, Corentin, Quentin, Benjamin, Damien, Fabien, Grégory, Jérémy, Ludovic, Mathieu, Rémi, Vincent, Xavier, '
               'Yann, Yannick, Aurélien, Bastien, Cédric, Cyril, Denis, Fabrice, Franck, Hervé, Lionel, Loïc, Régis, Thibault'),
        _uniqueWords('Martin, Bernard, Dubois, Thomas, Robert, Richard, Petit, Durand, Leroy, Moreau, Simon, Laurent, Lefebvre, Michel, '
               'Garcia, David, Bertrand, Roux, Vincent, Fournier, Morel, Girard, André, Mercier, Dupont, Lambert, Bonnet, François, '
               'Martinez, Legrand, Garnier, Faure, Rousseau, Blanc, Guerin, Muller, Henry, Roussel, Nicolas, Perrin, Morin, Mathieu, '
               'Clement, Gauthier, Dumont, Lopez, Fontaine, Chevalier, Robin, Masson, Sanchez, Gerard, Nguyen, Boyer, Denis, Lemaire, '
               'Duval, Joly, Gautier, Roger, Roche, Roy, Noel, Meyer, Lucas, Meunier, Jean, Perez, Marchand, Dufour, Blanchard, '
               'Marie, Barbier, Brun, Dumas, Brunet, Schmitt, Leroux, Colin, Fernandez, Pierre, Renard, Arnaud, Rolland, Caron, '
               'Aubert, Giraud, Leclerc, Vidal, Bourgeois, Renaud, Lemoine, Picard, Gaillard, Philippe, Leclercq, Lacroix, Fabre, '
               'Dupuis, Olivier, Rodriguez, Da Silva, Hubert, Louis, Charles, Guillot, Riviere, Le Gall, Guillaume, Adam, Rey, '
               'Moulin, Gonzalez, Berger, Lecomte, Menard, Fleury, Deschamps, Carpentier, Julien, Benoit, Paris, Maillard, Marchal, '
               'Aubry, Vasseur, Le Roux, Renault, Jacquet, Collet, Prevost, Poirier, Charpentier, Royer, Huet, Baron, Dupuy, Pons, '
               'Paul, Laine, Carre, Breton, Remy, Schneider, Perrot, Guyot, Barre, Marty, Cousin, Boucher, Bailly, Collin, Hamon, '
               'Leblanc, Gilbert, Langlois, Lebrun, Besson, Leveque, Chauvin, Bertin, Laporte, Etienne, Boulanger, Tessier, Le Goff, '
               'Bouvier, Leger, Mallet, Cordier, Lejeune, Germain, Pelletier, Poulain, Delmas, Texier, Gomez, '
               'Hoarau, Bodin, Marechal, Joubert, Sauvage, Pasquier, Bouchet, Tanguy, Hardy, Ferrand, Lefevre, '
               'Navarro, Petitjean, Vallet, Rousset, Briand, Chevallier, Delaunay, Guichard, Leconte, Marion, Millet, Ollivier'),
        _uniqueWords('Amiens, Angers, Annecy, Avignon, Besançon, Brest, Caen, Colmar, Dijon, Grenoble, Limoges, Metz, Nancy, Nîmes, '
               'Orléans, Pau, Poitiers, Reims, Rouen, Tours, Agen, Albi, Arles, Arras, Auxerre, Bayonne, Beauvais, Belfort, Biarritz, '
               'Blois, Bordeaux, Boulogne, Bourges, Calais, Carcassonne, Chambéry, Chartres, Cherbourg, Clermont-Ferrand, Cognac, '
               'Dieppe, Douai, Dunkerque, Épinal, Évreux, Fontainebleau, Gap, La Rochelle, Laval, Le Havre, Le Mans, Lille, Lorient, '
               'Lyon, Mâcon, Marseille, Menton, Montauban, Montpellier, Mulhouse, Nantes, Narbonne, Nevers, Nice, Niort, Périgueux, '
               'Perpignan, Quimper, Rennes, Roanne, Saint-Brieuc, Saint-Malo, Saintes, Sète, Strasbourg, Tarbes, Toulon, Toulouse, '
               'Troyes, Valence, Vannes, Versailles, Vichy')),

    'es_ES': LargeLocale(
        _uniqueWords('Lucía, Sofía, Martina, María, Julia, Paula, Valeria, Emma, Daniela, Carla, Alba, Noa, Alma, Sara, Carmen, Vega, '
               'Lara, Mía, Valentina, Olivia, Claudia, Jimena, Lola, Chloe, Aitana, Abril, Ana, Laia, Triana, Elena, Candela, Alejandra, '
               'Irene, Marta, Nerea, Ainhoa, Andrea, Ariadna, Beatriz, Blanca, Celia, Cristina, Elsa, Eva, Gabriela, Inés, Isabel, '
               'Laura, Lorena, Manuela, Marina, Miriam, Natalia, Nuria, Patricia, Pilar, Raquel, Rocío, Rosa, Silvia, Sonia, Susana, '
               'Teresa, Verónica, Victoria, Yolanda, Ángela, Antonia, Amparo, Concepción, Dolores, Encarnación, Esperanza, Francisca, '
               'Josefa, Juana, Mercedes, Montserrat, Remedios, Rosario, Soledad, Begoña, Esther, Inmaculada, Lourdes, Margarita, '
               'Milagros, Nieves, Olga, Rebeca, Sandra, Tamara, Vanesa, Virginia, Agustina, Aurora, Clara, Diana, Estela, Fátima, '
               'Gloria, Itziar, Leire, Maite, Mónica, Noelia, Olaia, Rut, Salma, Tania, Zoe, Adriana, Ainara, Amaia, Arantxa, Iratxe, '
               'Maialen, Nahia, Uxue, Iria, Uxía, Xiana, Antía, Lía, Ona, Júlia, Mireia, Núria, Montse, Berta, Judit, Joana'),
        _uniqueWords('Hugo, Martín, Lucas, Mateo, Leo, Daniel, Alejandro, Pablo, Manuel, Álvaro, Adrián, David, Mario, Enzo, Diego, '
               'Marcos, Izan, Javier, Marco, Álex, Bruno, Oliver, Miguel, Thiago, Antonio, Marc, Carlos, Ángel, Juan, Gonzalo, Gael, '
               'Sergio, Nicolás, Dylan, Gabriel, Jorge, José, Adam, Liam, Eric, Samuel, Darío, Héctor, Luca, Iker, Amir, Rodrigo, '
               'Saúl, Víctor, Francisco, Iván, Jesús, Jaime, Aarón, Rubén, Ian, Guillermo, Erik, Mohamed, Julen, Luis, Pau, Unai, '
               'Rafael, Joel, Alberto, Pedro, Raúl, Aitor, Fernando, Andrés, Ignacio, Jordi, Josep, Xavier, Oriol, Arnau, Pol, Biel, '
               'Roger, Gerard, Albert, Ramón, Enrique, Emilio, Eduardo, Felipe, Ricardo, Roberto, Santiago, Tomás, Vicente, Agustín, '
               'Alfonso, Andoni, Asier, Borja, César, Cristian, Domingo, Esteban, Fausto, Federico, Gregorio, Gustavo, Ismael, '
               'Joaquín, Julián, Lorenzo, Marcelo, Mariano, Matías, Nacho, Óscar, Patricio, Rogelio, Salvador, Sebastián, Teodoro, '
               'Ulises, Valentín, Xabier, Íñigo, Koldo, Gorka, Mikel, Ander, Ibai, Xoán, Brais, Anxo, Uxío, Iago, Martiño'),
        _uniqueWords('García, Rodríguez, González, Fernández, López, Martínez, Sánchez, Pérez, Gómez, Martín, Jiménez, Ruiz, Hernández, '
               'Díaz, Moreno, Muñoz, Álvarez, Romero, Alonso, Gutiérrez, Navarro, Torres, Domínguez, Vázquez, Ramos, Gil, Ramírez, '
               'Serrano, Blanco, Molina, Morales, Suárez, Ortega, Delgado, Castro, Ortiz, Rubio, Marín, Sanz, Núñez, Iglesias, Medina, '
               'Garrido, Cortés, Castillo, Santos, Lozano, Guerrero, Cano, Prieto, Méndez, Cruz, Calvo, Gallego, Vidal, León, Márquez, '
               'Herrera, Peña, Flores, Cabrera, Campos, Vega, Fuentes, Carrasco, Diez, Caballero, Reyes, Nieto, Aguilar, Pascual, '
               'Santana, Herrero, Lorenzo, Montero, Hidalgo, Giménez, Ibáñez, Ferrer, Durán, Santiago, Benítez, Mora, Vicente, '
               'Vargas, Arias, Carmona, Crespo, Román, Pastor, Soto, Sáez, Velasco, Moya, Soler, Parra, Esteban, Bravo, Gallardo, '
               'Rojas, Pardo, Merino, Franco, Espinosa, Izquierdo, Lara, Rivas, Silva, Rivera, Casado, Arroyo, Redondo, Camacho, Rey, '
               'Vera, Otero, Luque, Galán, Montes, Ríos, Sierra, Segura, Carrillo, Marcos, Marti, Soriano, Mendoza, Robles, Bernal, '
               'Vila, Valero, Palacios, Pereira, Exposito, Benito, Varela, Andrés, Heredia, Bueno, Rosa, Contreras, Guerra, Mateo, '
               'Villar, Trujillo, Bermúdez, Miranda, Aranda, Plaza, Escobar, Salazar, Beltrán, Quintana, Barrera, '
               'Ballesteros, Rosales, Cuesta, Alarcón, Pozo, Hurtado, Toledo, Sancho, Gallo, Zamora, Rincón, Collado, Padilla, Mesa, '
               'Abad, Aparicio, Barrios, Cordero, Del Río, Echevarría, Etxeberria, Garmendia, Goikoetxea, Ibarra, Larrañaga, '
               'Mendizábal, Olano, Urrutia, Zubiri, Castells, Puig, Pujol, Roca, Serra, Riera, Font, Sala, Casals, Bosch, Costa, '
               'Leal, Luna, Miralles, Nadal, Oliva, Pons, Rius, Sola, Tomás, Valls, Vives'),
        _uniqueWords('Albacete, Alicante, Badajoz, Burgos, Cáceres, Cádiz, Córdoba, Gijón, Girona, Granada, Huelva, León, Logroño, '
               'Lugo, Oviedo, Salamanca, Santander, Segovia, Toledo, Zamora, Almería, Ávila, Badalona, Barcelona, Bilbao, Castellón, '
               'Ceuta, Ciudad Real, Cuenca, Elche, Ferrol, Getafe, Guadalajara, Huesca, Ibiza, Jaén, Jerez, La Coruña, Las Palmas, '
               'Lleida, Madrid, Málaga, Marbella, Mérida, Murcia, Ourense, Palencia, Palma, Pamplona, Ponferrada, Pontevedra, Reus, '
               'Ronda, Sabadell, San Sebastián, Santiago de Compostela, Sevilla, Soria, Tarragona, Terrassa, Teruel, Valencia, '
               'Valladolid, Vigo, Vitoria, Zaragoza, Alcalá de Henares, Algeciras, Benidorm, Cartagena, Gandía, Linares, Lorca, '
               'Manresa, Mataró, Orihuela, Talavera, Torrevieja, Úbeda')),

    'pt_BR': LargeLocale(
        _uniqueWords('Helena, Alice, Laura, Maria, Valentina, Heloísa, Sophia, Manuela, Júlia, Isabela, Lívia, Beatriz, Cecília, '
               'Lorena, Maitê, Elisa, Antonella, Liz, Mariana, Giovanna, Melissa, Yasmin, Lara, Luiza, Ana, Clara, Isadora, Esther, '
               'Rafaela, Emanuelly, Lavínia, Agatha, Catarina, Gabriela, Nicole, Olívia, Rebeca, Sarah, Vitória, Bianca, Larissa, '
               'Letícia, Amanda, Bruna, Camila, Carolina, Daniela, Fernanda, Jéssica, Juliana, Natália, Patrícia, Priscila, Renata, '
               'Tatiana, Vanessa, Aline, Adriana, Andréa, Cláudia, Cristina, Débora, Elaine, Fabiana, Flávia, Gisele, Kelly, '
               'Luciana, Márcia, Mônica, Paula, Raquel, Sandra, Simone, Sônia, Silvana, Tânia, Vera, Aparecida, Benedita, '
               'Conceição, Francisca, Joana, Josefa, Lúcia, Marlene, Raimunda, Rita, Rosa, Terezinha, Antônia, Zélia, Ivone, Neide, '
               'Regina, Rosângela, Solange, Valéria, Eduarda, Stella, Pietra, Ayla, Aurora, Eloá, Mirella, Milena, Malu, Pérola, '
               'Brenda, Caroline, Glória, Jaqueline, Kátia, Lilian, Michele, Nathália, Roberta, Sabrina, Thaís'),
        _uniqueWords('Miguel, Arthur, Gael, Heitor, Theo, Davi, Gabriel, Bernardo, Samuel, João, Pedro, Lucas, Rafael, Matheus, '
               'Gustavo, Felipe, Enzo, Lorenzo, Benjamin, Isaac, Anthony, Bryan, Murilo, Nicolas, Leonardo, Benício, Joaquim, '
               'Henrique, Eduardo, Daniel, Vicente, Caio, Bento, Otávio, Thiago, Vinícius, Bruno, Rodrigo, Diego, Leandro, Marcelo, '
               'Fábio, André, Alexandre, Carlos, Fernando, Ricardo, Marcos, Paulo, Roberto, Sérgio, Luiz, Antônio, José, Francisco, '
               'Raimundo, Sebastião, Manoel, Geraldo, Benedito, Severino, Edson, Wellington, Anderson, Cleber, '
               'Everton, Jefferson, Robson, Wagner, Willian, Renan, Igor, Victor, Breno, Cauã, Kauê, Ravi, Yuri, Emanuel, Augusto, '
               'Cristiano, Danilo, Douglas, Elias, Fabrício, Flávio, Guilherme, Hugo, Ivan, Júlio, Juliano, Leonel, Luciano, Márcio, '
               'Maurício, Nelson, Osvaldo, Renato, Rogério, Sandro, Silvio, Ulisses, Valter, Wilson, Washington, '
               'Raul, Ruan, Lucca, Pietro, Valentim, Apollo, Ian, Noah, Levi, Asafe, Martin, Emanuel'),
        _uniqueWords('Silva, Santos, Oliveira, Souza, Rodrigues, Ferreira, Alves, Pereira, Lima, Gomes, Costa, Ribeiro, Martins, '
               'Carvalho, Almeida, Lopes, Soares, Fernandes, Vieira, Barbosa, Rocha, Dias, Nascimento, Andrade, Moreira, Nunes, '
               'Marques, Machado, Mendes, Freitas, Cardoso, Ramos, Gonçalves, Santana, Teixeira, Araújo, Pinto, Correia, Cavalcanti, '
               'Monteiro, Moura, Batista, Campos, Rezende, Castro, Azevedo, Reis, Melo, Cunha, Barros, Medeiros, Farias, Pires, '
               'Miranda, Fonseca, Viana, Duarte, Sales, Brito, Borges, Siqueira, Coelho, Tavares, Xavier, Macedo, Peixoto, Leite, '
               'Aguiar, Bezerra, Sampaio, Guimarães, Mota, Pacheco, Morais, Queiroz, Assis, Caldeira, Bastos, Diniz, Figueiredo, '
               'Franco, Godoy, Lacerda, Magalhães, Matos, Menezes, Nogueira, Paiva, Prado, Quintana, Rangel, Sá, Seixas, Toledo, '
               'Valente, Vasconcelos, Veloso, Amaral, Arruda, Bittencourt, Braga, Cabral, Camargo, Chaves, Coutinho, Esteves, '
               'Falcão, Fagundes, Garcia, Henriques, Jardim, Junqueira, Lobo, Maia, Mattos, Meireles, Mesquita, Neves, Novaes, '
               'Pedrosa, Porto, Portela, Rios, Sobral, Tenório, Torres, Vidal, Zanetti, Bertoldi, Bianchini, Conti, Ferraz, Lucena, '
               'Marinho, Mendonça, Prates, Rosa, Sarmento, Silveira, Simões, Teles, Uchoa, Vargas, Vilela, Abreu, Brandão, Carneiro, '
               'Couto, Cruz, Dantas, Estrela, Feitosa, Galvão, Holanda, Lins, Lisboa, Loureiro, Lyra, Mascarenhas, Medina, '
               'Paes, Pimentel, Quaresma, Ribas, Sant’Anna, Serpa, Souto, Tomé, Varela, Viegas, Werneck'),
        _uniqueWords('Aracaju, Belém, Blumenau, Campinas, Cuiabá, Curitiba, Florianópolis, Goiânia, Joinville, Londrina, Maceió, '
               'Manaus, Natal, Niterói, Olinda, Petrópolis, Santos, Sorocaba, Uberlândia, Vitória, Anápolis, Bauru, Belo Horizonte, '
               'Boa Vista, Brasília, Campina Grande, Campo Grande, Caruaru, Caxias do Sul, Chapecó, Contagem, Feira de Santana, '
               'Foz do Iguaçu, Franca, Fortaleza, Guarulhos, Ilhéus, Itajaí, Jaboatão, João Pessoa, Juiz de Fora, Macapá, Maringá, '
               'Mossoró, Ouro Preto, Palmas, Paraty, Pelotas, Petrolina, Piracicaba, Porto Alegre, Porto Velho, Recife, Ribeirão '
               'Preto, Rio Branco, Rio de Janeiro, Salvador, Santa Maria, Santarém, São Carlos, São José dos Campos, São Luís, '
               'São Paulo, Teresina, Uberaba, Volta Redonda')),

    'it_IT': LargeLocale(
        _uniqueWords('Sofia, Aurora, Giulia, Ginevra, Beatrice, Alice, Emma, Vittoria, Ludovica, Matilde, Chiara, Anna, Greta, Martina, '
               'Giorgia, Sara, Bianca, Gaia, Nicole, Camilla, Rebecca, Noemi, Arianna, Viola, Elena, Adele, Cecilia, Francesca, '
               'Elisa, Mia, Azzurra, Margherita, Marta, Irene, Isabel, Diana, Eleonora, Federica, Valentina, Alessia, Silvia, Laura, '
               'Paola, Roberta, Simona, Barbara, Monica, Cristina, Daniela, Elisabetta, Emanuela, Manuela, Michela, Patrizia, '
               'Raffaella, Sabrina, Serena, Stefania, Tiziana, Antonella, Claudia, Donatella, Fabiola, Gabriella, Lucia, Luisa, '
               'Marina, Nadia, Ornella, Rita, Rosa, Rosanna, Teresa, Angela, Carla, Franca, Giovanna, Giuseppina, Grazia, Lina, '
               'Maria, Pina, Rosaria, Carmela, Concetta, Assunta, Filomena, Immacolata, Annunziata, Agnese, Benedetta, Carolina, '
               'Caterina, Costanza, Elettra, Flavia, Gemma, Gioia, Ilaria, Letizia, Lucrezia, Maddalena, Miriam, Ottavia, Priscilla, '
               'Rachele, Sveva, Tecla, Vera, Virginia, Zoe, Alessandra, Anastasia, Debora, Erica, Fiorella, Ilenia, Jessica, Lara'),
        _uniqueWords('Leonardo, Francesco, Tommaso, Edoardo, Alessandro, Lorenzo, Mattia, Gabriele, Riccardo, Diego, Nicolò, Matteo, '
               'Giuseppe, Antonio, Federico, Pietro, Giovanni, Filippo, Samuele, Davide, Christian, Marco, Luca, Simone, Michele, '
               'Andrea, Stefano, Paolo, Roberto, Giorgio, Massimo, Claudio, Fabio, Maurizio, Daniele, Sergio, Alberto, Franco, '
               'Mario, Luigi, Salvatore, Vincenzo, Carlo, Domenico, Bruno, Enrico, Gianni, Giancarlo, Gianluca, Gianfranco, Nicola, '
               'Raffaele, Emanuele, Fabrizio, Marcello, Massimiliano, Mauro, Piero, Renato, Valerio, Vittorio, Alessio, Cristiano, '
               'Dario, Emilio, Ettore, Fausto, Flavio, Gino, Guido, Ivano, Lino, Lucio, Mirko, Nando, Orazio, Ottavio, Rocco, Romeo, '
               'Rosario, Sandro, Silvio, Tiziano, Ugo, Umberto, Walter, Achille, Agostino, Alfredo, Amedeo, Angelo, Aldo, Arturo, '
               'Attilio, Beniamino, Biagio, Carmine, Cesare, Corrado, Dante, Elia, Ernesto, Ezio, Felice, Gaetano, Gerardo, Ignazio, '
               'Jacopo, Libero, Manuel, Martino, Nazzareno, Osvaldo, Pasquale, Raimondo, Saverio, Teodoro, Vito, Zeno, Brando'),
        _uniqueWords('Rossi, Russo, Ferrari, Esposito, Bianchi, Romano, Colombo, Ricci, Marino, Greco, Bruno, Gallo, Conti, De Luca, '
               'Mancini, Costa, Giordano, Rizzo, Lombardi, Moretti, Barbieri, Fontana, Santoro, Mariani, Rinaldi, Caruso, Ferrara, '
               'Galli, Martini, Leone, Longo, Gentile, Martinelli, Vitale, Lombardo, Serra, Coppola, De Santis, D\'Angelo, Marchetti, '
               'Parisi, Villa, Conte, Ferraro, Ferri, Fabbri, Bianco, Marini, Grasso, Valentini, Messina, Sala, De Angelis, Gatti, '
               'Pellegrini, Palumbo, Sanna, Farina, Rizzi, Monti, Cattaneo, Morelli, Amato, Silvestri, Mazza, Testa, Grassi, '
               'Pellegrino, Carbone, Giuliani, Benedetti, Barone, Rossetti, Caputo, Montanari, Guerra, Palmieri, Bernardi, Martino, '
               'Fiore, De Rosa, Ferretti, Bellini, Basile, Riva, Donati, Piras, Vitali, Battaglia, Sartori, Neri, Costantini, '
               'Milani, Pagano, Ruggiero, Sorrentino, D\'Amico, Orlando, Damico, Negri, Fumagalli, Ferrero, Bassi, '
               'Bellucci, Bertolini, Brambilla, Bruni, Cantoni, Capriotti, Carli, Casadei, Castelli, Cavalli, Ceccarelli, Cirillo, '
               'Colonna, Corsi, Cristiani, D\'Agostino, Del Monte, Di Stefano, Esposti, Fabbro, Fanelli, Fiorentino, Galante, '
               'Gentili, Ghidini, Giannini, Guidi, Iannone, Landi, Lanza, Leonardi, Lucchesi, Maggi, Mancuso, Marchi, Mele, Mondini, '
               'Monaco, Montalto, Mura, Nardi, Olivieri, Orsini, Pace, Pagani, Pastore, Perrone, Piazza, Poli, Porcu, Puglisi, '
               'Quaranta, Raimondi, Rocca, Romagnoli, Salvi, Santini, Sartini, Scotti, Sereni, Spina, Tedesco, '
               'Tosi, Toscano, Tuccillo, Valenti, Vannucci, Venturi, Zanetti, Zanella, Zeni'),
        _uniqueWords('Ancona, Arezzo, Bergamo, Bologna, Brescia, Cagliari, Como, Cremona, Ferrara, Lecce, Lucca, Mantova, Modena, '
               'Padova, Parma, Perugia, Pisa, Ravenna, Siena, Trento, Alessandria, Aosta, Asti, Bari, Benevento, Bolzano, Brindisi, '
               'Caserta, Catania, Catanzaro, Cesena, Cosenza, Cuneo, Firenze, Foggia, Forlì, Genova, Grosseto, L\'Aquila, La Spezia, '
               'Latina, Livorno, Macerata, Matera, Messina, Milano, Monza, Napoli, Novara, Palermo, Pavia, Pesaro, Pescara, Piacenza, '
               'Pistoia, Potenza, Prato, Ragusa, Reggio Emilia, Rimini, Roma, Rovigo, Salerno, Sassari, Savona, Siracusa, Taranto, '
               'Terni, Torino, Trapani, Treviso, Trieste, Udine, Urbino, Varese, Venezia, Verona, Vicenza, Viterbo')),

    'nl_NL': LargeLocale(
        _uniqueWords('Emma, Julia, Mila, Tess, Sophie, Zoë, Sara, Nora, Yara, Eva, Liv, Anna, Lotte, Saar, Noor, Fenna, Lieke, Isa, '
               'Evi, Lisa, Femke, Fleur, Roos, Sanne, Lynn, Maud, Iris, Jasmijn, Anne, Esmee, Floor, Ilse, Lisanne, Marit, Merel, '
               'Nina, Puck, Romy, Sophia, Vera, Amber, Britt, Demi, Eline, Elise, Hanna, Indy, Jade, Kiki, Lara, Lauren, Luna, '
               'Manon, Nikki, Olivia, Quinty, Sterre, Suze, Tara, Veerle, Ylva, Marieke, Annemieke, Ingrid, Monique, '
               'Petra, Sandra, Wendy, Linda, Marjolein, Esther, Karin, Mirjam, Nicole, Patricia, Jolanda, Ellen, Anja, Bianca, '
               'Carla, Diana, Elly, Gerda, Hilda, Ineke, Joke, Lies, Marga, Nel, Riet, Tineke, Truus, Wilma, Antje, Baukje, Dieuwke, '
               'Fokje, Geertje, Hinke, Janneke, Klaasje, Liesbeth, Marijke, Neeltje, Renske, Sietske, Trijntje, Wietske, Aaltje, '
               'Femmy, Hester, Josefien, Mieke, Rianne, Sjoukje, Willemijn, Lieve, Pien, Fay, Benthe, Ise, Jill, Elin, Feline'),
        _uniqueWords('Noah, Luca, Sem, Lucas, Levi, Finn, Daan, Milan, Bram, Mees, Jesse, Thijs, Liam, Adam, Noud, Siem, Teun, Max, '
               'Sven, Lars, Ruben, Stijn, Tim, Tom, Thomas, Jens, Joep, Jurre, Koen, Luuk, Mats, Mick, '
               'Niels, Olivier, Rick, Roy, Sander, Sjoerd, Wouter, Bart, Bas, Dennis, Erik, Frank, Gerrit, Hans, Henk, Jan, Jeroen, '
               'Johan, Joost, Kees, Klaas, Marco, Martijn, Michiel, Paul, Peter, Pieter, Remco, Rob, Ronald, Ruud, Sjaak, Theo, '
               'Willem, Arjen, Bert, Cor, Dirk, Evert, Freek, Gert, Harm, Hendrik, Huub, Jaap, Jelle, Joris, Kasper, Leendert, Maarten, '
               'Menno, Nico, Otto, Pim, Rutger, Sietse, Tjeerd, Ward, Wessel, Wim, Yannick, Aart, Ad, Berend, Douwe, Folkert, Gijs, '
               'Hidde, Ids, Jorrit, Kick, Lammert, Marten, Oene, Popke, Reinier, Siebe, Taeke, Ubbo, Wybe, Hugo, Boaz, Floris, '
               'Guus, Jasper, Matthijs, Quinten, Rens, Sepp, Tijn, Valentijn, Vincent, Xander, Ties, Ravi, Dex'),
        _uniqueWords('de Jong, Jansen, de Vries, van den Berg, van Dijk, Bakker, Janssen, Visser, Smit, Meijer, de Boer, Mulder, '
               'de Groot, Bos, Vos, Peters, Hendriks, van Leeuwen, Dekker, Brouwer, de Wit, Dijkstra, Smits, de Graaf, van der Meer, '
               'van der Linden, Kok, Jacobs, de Haan, Vermeulen, van den Heuvel, van der Veen, van den Broek, de Bruijn, de Bruin, '
               'van der Heijden, Schouten, van Beek, Willems, van Vliet, van de Ven, Hoekstra, Maas, Verhoeven, Koster, van Dam, '
               'van der Wal, Prins, Blom, Huisman, Peeters, de Jonge, Kuipers, van Veen, Post, Kuiper, Veenstra, Kramer, van den Brink, '
               'Scholten, van Wijk, Postma, Martens, Vink, de Ruiter, Timmermans, Groen, Gerritsen, Jonker, van Loon, Boer, '
               'van der Velde, Willemsen, Smeets, de Lange, de Vos, Bosch, van Dongen, Schipper, de Koning, van der Laan, Koning, '
               'van der Velden, Driessen, van Doorn, Hermans, Evers, van den Bosch, van der Meulen, Hofman, Bosman, Wolters, '
               'Sanders, van der Horst, Mol, Kuijpers, Molenaar, van de Pol, de Leeuw, Verbeek, Aalbers, Admiraal, Beekman, Bergsma, '
               'Boersma, Bouma, Brink, Claassen, Damen, de Beer, de Kok, de Wolf, Doornbos, Elzinga, Faber, Feenstra, Geurts, '
               'Haagsma, Heijnen, Holtkamp, Hoogland, Hulshof, Kamphuis, Keizer, Kloosterman, Kooistra, Lammers, Lubbers, Meijers, '
               'Nijhuis, Oosterhuis, Oostra, Pronk, Rijnders, Rutten, Sijbrandij, Sluiter, Stam, Steenbergen, Tuinstra, van Assen, '
               'van Buren, van Delft, van Gelder, van Gils, van Hout, van Kampen, van Lent, van Oers, van Rijn, '
               'van Schaik, van Vugt, Veldhuis, Verhagen, Vlaar, Vonk, Westra, Wiersma, Wijnands, Zijlstra, Zwart'),
        _uniqueWords('Alkmaar, Amersfoort, Apeldoorn, Arnhem, Breda, Delft, Deventer, Dordrecht, Enschede, Gouda, Groningen, Haarlem, '
               'Leeuwarden, Leiden, Maastricht, Nijmegen, Tilburg, Utrecht, Zwolle, Zaandam, Almere, Amstelveen, Amsterdam, Assen, '
               'Bergen op Zoom, Den Bosch, Den Haag, Den Helder, Doetinchem, Drachten, Ede, Eindhoven, Emmen, Harderwijk, Heerenveen, '
               'Heerlen, Helmond, Hengelo, Hilversum, Hoorn, Kampen, Katwijk, Lelystad, Middelburg, Nieuwegein, Oss, Purmerend, '
               'Roermond, Roosendaal, Rotterdam, Schiedam, Sittard, Sneek, Venlo, Vlaardingen, Vlissingen, Waalwijk, Weert, '
               'Zeist, Zoetermeer, Zutphen, Harlingen, Hoogeveen, Meppel, Steenwijk, Terneuzen, Tiel, Veenendaal, Woerden')),
    }


def _genders() -> Tuple[FrozenSet[str], FrozenSet[str]]:
    """Every locale's first names folded to lower case, as (female, male),
    with a name either list holds in any locale left out of both: a name
    tells a gender only where no list disagrees.
    """

    female = {name.casefold() for locale in LARGE_LOCALES.values() for name in locale.femaleNames}
    male = {name.casefold() for locale in LARGE_LOCALES.values() for name in locale.maleNames}
    both = female & male

    return frozenset(female - both), frozenset(male - both)


FEMALE_NAMES, MALE_NAMES = _genders()


def _international() -> LargeLocale:
    """Every locale's lists together, for `lists: 2` with no `locale`, as
    the default lists are an international mix.
    """

    def joined(field: str) -> Tuple[str, ...]:
        return tuple(dict.fromkeys(name for locale in LARGE_LOCALES.values() for name in getattr(locale, field)))

    return LargeLocale(joined('femaleNames'), joined('maleNames'), joined('lastNames'), joined('cities'))


LARGE_DEFAULT_LOCALE = _international()
